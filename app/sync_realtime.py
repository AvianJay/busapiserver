from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import INTERCITY_CITY_NAME, Settings, get_settings, guess_city_from_routeid, to_intercity_routeid
from app.db import (
    get_connection,
    init_db,
    load_route_static,
    load_tdx_fetch_state,
    route_exists,
    save_tdx_fetch_state,
)
from app.logging_utils import get_logger, setup_logging, shutdown_logging
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient, TDXJSONResponse


STOP_STATUS_MESSAGES = {
    1: "\u5c1a\u672a\u767c\u8eca",
    2: "\u4ea4\u7ba1\u4e0d\u505c\u9760",
    3: "末班駛離",
    4: "\u4eca\u65e5\u672a\u71df\u904b",
}

ARRIVING_VEHICLE_STOP_STATUS = 1
REALTIME_FETCH_STATE_RETENTION_SECONDS = 86400
LOGGER = get_logger("sync_realtime")


@dataclass
class PlateObservation:
    plate: str
    pathid: int
    stopid: str
    eta: int | None
    is_arriving: bool


class RouteNotFoundError(KeyError):
    """Raised when a route is missing from the local database."""


@dataclass
class CacheEntry:
    snapshot: dict[str, Any]
    expires_at: float


@dataclass
class BusesCacheEntry:
    buses: list[dict[str, Any]]
    expires_at: float


def _tdx_routeid_to_local(city: str, routeid: Any) -> str | None:
    if routeid is None:
        return None
    normalized = str(routeid).strip()
    if not normalized:
        return None
    if city == INTERCITY_CITY_NAME:
        return to_intercity_routeid(normalized)
    return normalized


def _to_unix_seconds(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _to_hhmm(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt.strftime("%H:%M")


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _adjusted_eta(
    data: dict[str, Any],
    *,
    now_ts: int,
    fallback_update_time: str | None = None,
) -> int | None:
    estimate_time = _to_int_or_none(data.get("EstimateTime"))
    if estimate_time is None:
        return None

    update_time = (
        data.get("SrcUpdateTime")
        or data.get("UpdateTime")
        or fallback_update_time
    )
    update_ts = _to_unix_seconds(update_time)
    if update_ts is None:
        return estimate_time

    eta = estimate_time - (now_ts - update_ts)
    return max(-1, eta)


def _build_message(item: dict[str, Any], *, now_ts: int) -> str:
    if _adjusted_eta(item, now_ts=now_ts) is not None:
        return ""

    stop_status = _to_int_or_none(item.get("StopStatus"))
    if stop_status == 1:
        scheduled_time = (item.get("ScheduledTime") or "").strip()
        if scheduled_time:
            return scheduled_time
        next_bus_time = _to_hhmm(item.get("NextBusTime"))
        if next_bus_time:
            return next_bus_time
        return STOP_STATUS_MESSAGES[1]

    if stop_status in {2, 3, 4}:
        return STOP_STATUS_MESSAGES[stop_status]

    if item.get("IsLastBus"):
        return STOP_STATUS_MESSAGES[3]

    scheduled_time = (item.get("ScheduledTime") or "").strip()
    if scheduled_time:
        return scheduled_time

    return ""


def _normalize_plate(value: Any) -> str | None:
    if value is None:
        return None
    plate = str(value).strip()
    if not plate or plate == "-1":
        return None
    return plate


def _append_stop_eta(
    stop_bucket: dict[str, Any],
    *,
    plate: str | None,
    eta: int | None,
    is_arriving: bool,
) -> None:
    if eta is None:
        return
    stop_bucket.setdefault("etas", []).append(
        {
            "plate": plate,
            "eta": eta,
            "is_arriving": is_arriving,
        }
    )


def _finalize_stop_eta_list(stop_bucket: dict[str, Any]) -> None:
    raw_etas = stop_bucket.get("etas") or []
    if not isinstance(raw_etas, list) or not raw_etas:
        stop_bucket["etas"] = []
        return

    deduped: dict[str, dict[str, Any]] = {}
    for entry in raw_etas:
        if not isinstance(entry, dict):
            continue

        plate = _normalize_plate(entry.get("plate"))
        eta = _to_int_or_none(entry.get("eta"))
        is_arriving = bool(entry.get("is_arriving"))
        if eta is None:
            continue

        key = plate or f"anon:{eta}"
        candidate = {
            "plate": plate,
            "eta": eta,
            "is_arriving": is_arriving,
        }
        current = deduped.get(key)
        if current is None:
            deduped[key] = candidate
            continue

        current_score = (
            0 if current.get("is_arriving") else 1,
            current.get("eta") if current.get("eta") is not None else 10**9,
        )
        candidate_score = (
            0 if candidate.get("is_arriving") else 1,
            candidate.get("eta") if candidate.get("eta") is not None else 10**9,
        )
        if candidate_score < current_score:
            deduped[key] = candidate

    stop_bucket["etas"] = sorted(
        deduped.values(),
        key=lambda entry: (
            0 if entry.get("is_arriving") else 1,
            entry.get("eta") if entry.get("eta") is not None else 10**9,
            entry.get("plate") or "",
        ),
    )

    if stop_bucket.get("eta") is None:
        stop_bucket["eta"] = next(
            (
                entry.get("eta")
                for entry in stop_bucket["etas"]
                if entry.get("eta") is not None
            ),
            None,
        )
    if stop_bucket.get("eta") is not None:
        stop_bucket["message"] = ""


def _collect_plate_observations(
    item: dict[str, Any],
    *,
    pathid: int,
    stopid: str,
    now_ts: int,
) -> list[PlateObservation]:
    observations: list[PlateObservation] = []

    top_plate = _normalize_plate(item.get("PlateNumb"))
    top_eta = _adjusted_eta(item, now_ts=now_ts)
    top_is_arriving = _to_int_or_none(item.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
    if top_plate is not None and top_eta is not None:
        observations.append(
            PlateObservation(
                plate=top_plate,
                pathid=pathid,
                stopid=stopid,
                eta=top_eta,
                is_arriving=top_is_arriving,
            )
        )

    for estimate in item.get("Estimates") or []:
        estimate_plate = _normalize_plate(estimate.get("PlateNumb"))
        estimate_eta = _adjusted_eta(
            estimate,
            now_ts=now_ts,
            fallback_update_time=item.get("SrcUpdateTime") or item.get("UpdateTime"),
        )
        if estimate_plate is None or estimate_eta is None:
            continue
        estimate_is_arriving = (
            _to_int_or_none(estimate.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
        )
        observations.append(
            PlateObservation(
                plate=estimate_plate,
                pathid=pathid,
                stopid=stopid,
                eta=estimate_eta,
                is_arriving=estimate_is_arriving,
            )
        )

    return observations


def _batched_resource_key(kind: str, city: str, routeids: list[str]) -> str:
    joined = "\n".join(routeids).encode("utf-8")
    digest = hashlib.sha1(joined).hexdigest()[:16]
    return f"{kind}:{city}:{len(routeids)}:{digest}"


def _persist_fetch_state(
    connection,
    resource_key: str,
    response: TDXJSONResponse,
    checked_at: int,
    previous_state: dict | None,
) -> None:
    previous_last_modified = None if previous_state is None else previous_state.get("last_modified")
    previous_updated_at = None if previous_state is None else previous_state.get("last_updated_at")
    effective_last_modified = response.last_modified or previous_last_modified

    save_tdx_fetch_state(
        connection,
        resource_key,
        last_modified=effective_last_modified,
        last_status=response.status_code,
        last_checked_at=checked_at,
        last_updated_at=checked_at if not response.not_modified else previous_updated_at,
    )


def _prune_old_realtime_fetch_state(connection, now: int) -> None:
    connection.execute(
        """
        DELETE FROM tdx_fetch_state
        WHERE (
            resource_key LIKE 'realtime_eta:%'
            OR resource_key LIKE 'realtime_buses:%'
        )
          AND last_checked_at < ?
        """,
        (now - REALTIME_FETCH_STATE_RETENTION_SECONDS,),
    )


def _build_buses_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buses_by_plate: dict[str, dict[str, Any]] = {}

    for item in items:
        if _to_int_or_none(item.get("DutyStatus")) == 2:
            continue

        plate = _normalize_plate(item.get("PlateNumb"))
        if plate is None:
            continue

        position = item.get("BusPosition") or {}
        lat_raw = position.get("PositionLat")
        lon_raw = position.get("PositionLon")
        if lat_raw is None or lon_raw is None:
            continue

        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (TypeError, ValueError):
            continue

        bus = {
            "id": plate,
            "direction": item.get("Direction"),
            "lat": lat,
            "lon": lon,
            "speed": item.get("Speed"),
            "azimuth": item.get("Azimuth"),
            "status": item.get("BusStatus"),
            "time": _to_unix_seconds(item.get("GPSTime")),
        }

        existing = buses_by_plate.get(plate)
        if existing is None:
            buses_by_plate[plate] = bus
            continue

        existing_time = existing.get("time")
        next_time = bus.get("time")
        if next_time is not None and (existing_time is None or next_time > existing_time):
            buses_by_plate[plate] = bus

    return list(buses_by_plate.values())


class RealtimeService:
    def __init__(self, settings: Settings, client: TDXClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[str, CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._tracked_routes: dict[str, dict[str, float]] = {}
        self._tracked_routes_lock = threading.Lock()
        self._city_refresh_locks: dict[str, threading.Lock] = {}
        self._city_refresh_locks_guard = threading.Lock()

    def get_snapshot(self, routeid: str, *, force_refresh: bool = False) -> dict[str, Any]:
        static_route = self._load_static_route(routeid)
        if static_route is None:
            raise RouteNotFoundError(routeid)

        city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if city is None:
            return self._get_single_route_snapshot(routeid, static_route, force_refresh=force_refresh)

        self._track_route(city, routeid)

        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        city_lock = self._get_city_refresh_lock(city)
        with city_lock:
            if not force_refresh:
                cached = self._get_cached(routeid)
                if cached is not None:
                    return cached

            try:
                self._refresh_city_cache(city, routeid, static_route, force_refresh=force_refresh)
            except Exception:
                stale = self._get_cached(routeid, allow_expired=True)
                if stale is not None:
                    LOGGER.warning(
                        "using stale realtime cache routeid=%s city=%s after refresh failure",
                        routeid,
                        city,
                    )
                    self._set_cached(routeid, stale)
                    return stale
                raise

            cached = self._get_cached(routeid, allow_expired=True)
            if cached is not None:
                self._set_cached(routeid, cached)
                return cached

        return self._build_snapshot(routeid, static_route, [])

    def get_batch_snapshots(self, routeids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch realtime snapshots for multiple route IDs at once.

        Routes are grouped by city so each city generates a single TDX batch
        request, drastically reducing the number of HTTP calls compared to
        requesting each route individually.  Results are served from the
        per-route cache when fresh; only routes whose cache entries are stale
        or missing trigger a TDX refresh.

        Returns a mapping of routeid -> snapshot for every route that was
        successfully resolved.  Unknown route IDs are silently omitted.
        """
        deduped_routeids = sorted(set(routeids))
        if not deduped_routeids:
            return {}

        # Pre-populate from cache.
        results: dict[str, dict[str, Any]] = {}
        routes_needing_refresh: list[str] = []
        for routeid in deduped_routeids:
            cached = self._get_cached(routeid)
            if cached is not None:
                results[routeid] = cached
            else:
                routes_needing_refresh.append(routeid)

        if not routes_needing_refresh:
            return results

        # Group routes that still need a refresh by city.
        routes_by_city: dict[str, list[str]] = {}
        for routeid in routes_needing_refresh:
            city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
            if city is not None:
                routes_by_city.setdefault(city, []).append(routeid)

        # Refresh each city group.
        for city, city_routeids in routes_by_city.items():
            # Ensure all routes are tracked so _refresh_city_cache_for_routes
            # includes them in the batch.
            for routeid in city_routeids:
                self._track_route(city, routeid)

            city_lock = self._get_city_refresh_lock(city)
            with city_lock:
                try:
                    self._refresh_city_cache_for_routes(city, city_routeids)
                except Exception:
                    LOGGER.warning(
                        "batch realtime refresh failed for city=%s routes=%s",
                        city,
                        len(city_routeids),
                    )

            # Pick up whatever landed in cache (including stale fallbacks).
            for routeid in city_routeids:
                cached = self._get_cached(routeid, allow_expired=True)
                if cached is not None:
                    self._set_cached(routeid, cached)
                    results[routeid] = cached

        # Handle routes without a known city (single-route fallback).
        for routeid in routes_needing_refresh:
            if routeid in results:
                continue
            city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
            if city is not None:
                continue  # already handled above

            static_route = self._load_static_route(routeid)
            if static_route is None:
                continue
            try:
                snapshot = self._get_single_route_snapshot(
                    routeid, static_route, force_refresh=False,
                )
                results[routeid] = snapshot
            except Exception:
                LOGGER.warning(
                    "batch single-route fallback failed routeid=%s",
                    routeid,
                )

        return results

    def _refresh_city_cache_for_routes(
        self,
        city: str,
        routeids: list[str],
    ) -> None:
        """Refresh the cache for *exactly* the given routeids within *city*.

        Unlike ``_refresh_city_cache`` (which uses the tracked-routes set),
        this method operates on the explicit list supplied by the caller,
        making it suitable for batch endpoint use-cases where the caller
        already knows which routes they need.
        """
        static_routes: dict[str, dict[str, Any]] = {}
        with get_connection(self.settings.db_path) as connection:
            for routeid in routeids:
                static_route = load_route_static(connection, routeid)
                if static_route is not None:
                    static_routes[routeid] = static_route

        if not static_routes:
            return

        effective_routeids = sorted(static_routes)
        stale_snapshots = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_eta", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if all(snapshot is not None for snapshot in stale_snapshots.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_estimated_time_of_arrival_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "batch realtime not modified city=%s routes=%s",
                city,
                len(effective_routeids),
            )
            for routeid, snapshot in stale_snapshots.items():
                if snapshot is not None:
                    self._set_cached(routeid, snapshot)
            return

        LOGGER.info(
            "batch realtime refreshed city=%s routes=%s status=%s items=%s",
            city,
            len(effective_routeids),
            response.status_code,
            len(response.payload or []),
        )

        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_routeid_to_local(
                city,
                item.get("SubRouteUID") or item.get("RouteUID"),
            )
            if routeid in static_routes:
                items_by_route[routeid].append(item)

        for routeid, static_route in static_routes.items():
            snapshot = self._build_snapshot(routeid, static_route, items_by_route.get(routeid, []))
            self._set_cached(routeid, snapshot)

    def _refresh_city_cache(
        self,
        city: str,
        requested_routeid: str,
        requested_static_route: dict[str, Any],
        *,
        force_refresh: bool,
    ) -> None:
        routeids = self._get_tracked_routeids(city, include_routeid=requested_routeid)
        static_routes = self._load_static_routes(routeids, requested_routeid, requested_static_route)
        if requested_routeid not in static_routes:
            raise RouteNotFoundError(requested_routeid)

        effective_routeids = sorted(static_routes)
        stale_snapshots = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_eta", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if not force_refresh and all(snapshot is not None for snapshot in stale_snapshots.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_estimated_time_of_arrival_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if force_refresh or previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "realtime batch not modified city=%s routes=%s requested_routeid=%s",
                city,
                len(effective_routeids),
                requested_routeid,
            )
            for routeid, snapshot in stale_snapshots.items():
                if snapshot is not None:
                    self._set_cached(routeid, snapshot)
            return

        LOGGER.info(
            "realtime batch refreshed city=%s routes=%s requested_routeid=%s status=%s items=%s",
            city,
            len(effective_routeids),
            requested_routeid,
            response.status_code,
            len(response.payload or []),
        )

        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_routeid_to_local(
                city,
                item.get("SubRouteUID") or item.get("RouteUID"),
            )
            if routeid in static_routes:
                items_by_route[routeid].append(item)

        for routeid, static_route in static_routes.items():
            snapshot = self._build_snapshot(routeid, static_route, items_by_route.get(routeid, []))
            self._set_cached(routeid, snapshot)

    def _get_single_route_snapshot(
        self,
        routeid: str,
        static_route: dict[str, Any],
        *,
        force_refresh: bool,
    ) -> dict[str, Any]:
        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        try:
            items: list[dict[str, Any]] = []
            for city in self._candidate_cities_for_route(routeid):
                current_items = self.client.fetch_estimated_time_of_arrival(city, routeid)
                if current_items:
                    items = current_items
                    break
            snapshot = self._build_snapshot(routeid, static_route, items)
        except Exception:
            stale = self._get_cached(routeid, allow_expired=True)
            if stale is not None:
                LOGGER.warning("using stale realtime cache routeid=%s after single-route refresh failure", routeid)
                self._set_cached(routeid, stale)
                return stale
            raise

        self._set_cached(routeid, snapshot)
        return snapshot

    def _candidate_cities_for_route(self, routeid: str) -> list[str]:
        candidate_cities: list[str] = []
        guessed_city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if guessed_city:
            if guessed_city == INTERCITY_CITY_NAME:
                return [guessed_city]
            candidate_cities.append(guessed_city)
        for city in self.settings.tdx_cities:
            if city not in candidate_cities:
                candidate_cities.append(city)
        return candidate_cities

    def _track_route(self, city: str, routeid: str) -> None:
        self._get_tracked_routeids(city, include_routeid=routeid)

    def _get_tracked_routeids(self, city: str, *, include_routeid: str | None = None) -> list[str]:
        now = time.monotonic()
        expires_at = now + self.settings.realtime_track_ttl

        with self._tracked_routes_lock:
            city_routes = self._tracked_routes.setdefault(city, {})
            expired_routeids = [routeid for routeid, route_expires_at in city_routes.items() if route_expires_at < now]
            for routeid in expired_routeids:
                city_routes.pop(routeid, None)

            if include_routeid:
                city_routes[include_routeid] = expires_at

            if not city_routes:
                self._tracked_routes.pop(city, None)
                return []

            return sorted(city_routes)

    def _load_static_route(self, routeid: str) -> dict[str, Any] | None:
        with get_connection(self.settings.db_path) as connection:
            return load_route_static(connection, routeid)

    def _load_static_routes(
        self,
        routeids: list[str],
        requested_routeid: str,
        requested_static_route: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        static_routes: dict[str, dict[str, Any]] = {}
        with get_connection(self.settings.db_path) as connection:
            for routeid in routeids:
                if routeid == requested_routeid:
                    static_route = requested_static_route
                else:
                    static_route = load_route_static(connection, routeid)

                if static_route is None:
                    continue
                static_routes[routeid] = static_route

        return static_routes

    def _get_city_refresh_lock(self, city: str) -> threading.Lock:
        with self._city_refresh_locks_guard:
            return self._city_refresh_locks.setdefault(city, threading.Lock())

    def _build_snapshot(
        self,
        routeid: str,
        static_route: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now_ts = int(time.time())
        item_times = []
        for item in items:
            item_time = (
                _to_unix_seconds(item.get("UpdateTime"))
                or _to_unix_seconds(item.get("DataTime"))
                or _to_unix_seconds(item.get("SrcRecTime"))
                or _to_unix_seconds(item.get("SrcTransTime"))
                or _to_unix_seconds(item.get("SrcUpdateTime"))
                or _to_unix_seconds(item.get("TransTime"))
            )
            if item_time is not None:
                item_times.append(item_time)
        updated_at = max(item_times) if item_times else int(time.time())

        paths = static_route["paths"]
        grouped: dict[int, dict[str, dict[str, Any]]] = {}
        plate_candidates: dict[tuple[int, str], list[PlateObservation]] = {}

        for item in items:
            pathid = int(item.get("Direction") or 0)
            stopid = item.get("StopID") or item.get("StopUID")
            if not stopid:
                continue

            path_bucket = grouped.setdefault(pathid, {})
            stop_bucket = path_bucket.setdefault(
                stopid,
                {
                    "stopid": stopid,
                    "eta": None,
                    "message": "",
                    "buses": [],
                    "etas": [],
                },
            )

            estimate_time = _adjusted_eta(item, now_ts=now_ts)
            message = _build_message(item, now_ts=now_ts)
            if estimate_time is not None:
                if stop_bucket["eta"] is None or estimate_time < stop_bucket["eta"]:
                    stop_bucket["eta"] = estimate_time
                    stop_bucket["message"] = ""
            elif not stop_bucket["message"] and message:
                stop_bucket["message"] = message

            top_plate = _normalize_plate(item.get("PlateNumb"))
            top_is_arriving = (
                _to_int_or_none(item.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
            )
            _append_stop_eta(
                stop_bucket,
                plate=top_plate,
                eta=estimate_time,
                is_arriving=top_is_arriving,
            )

            for estimate in item.get("Estimates") or []:
                estimate_plate = _normalize_plate(estimate.get("PlateNumb"))
                estimate_eta = _adjusted_eta(
                    estimate,
                    now_ts=now_ts,
                    fallback_update_time=item.get("SrcUpdateTime") or item.get("UpdateTime"),
                )
                estimate_is_arriving = (
                    _to_int_or_none(estimate.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
                )
                _append_stop_eta(
                    stop_bucket,
                    plate=estimate_plate,
                    eta=estimate_eta,
                    is_arriving=estimate_is_arriving,
                )

            for observation in _collect_plate_observations(
                item,
                pathid=pathid,
                stopid=stopid,
                now_ts=now_ts,
            ):
                plate_candidates.setdefault((observation.pathid, observation.plate), []).append(observation)

        for (pathid, plate), observations in plate_candidates.items():
            arriving_observations = [item for item in observations if item.is_arriving]
            effective_observations = arriving_observations or observations
            path_meta = paths.get(pathid)
            stop_index = (path_meta or {}).get("stop_index", {})

            def _rank(observation: PlateObservation) -> tuple[int, int, str]:
                eta_rank = observation.eta if observation.eta is not None else 10**9
                seq_rank = stop_index.get(observation.stopid, {}).get("seq", 10**9)
                return (eta_rank, seq_rank, observation.stopid)

            selected = min(effective_observations, key=_rank)
            path_bucket = grouped.get(pathid)
            if not path_bucket:
                continue
            stop_bucket = path_bucket.get(selected.stopid)
            if not stop_bucket:
                continue
            if all(bus["id"] != plate for bus in stop_bucket["buses"]):
                stop_bucket["buses"].append({"id": plate, "type": "normal"})

        response_paths = []
        seen_pathids = set(paths)
        seen_pathids.update(grouped)

        for path_bucket in grouped.values():
            for stop_bucket in path_bucket.values():
                _finalize_stop_eta_list(stop_bucket)

        for pathid in sorted(seen_pathids):
            path_meta = paths.get(pathid)
            stop_entries = list((grouped.get(pathid) or {}).values())

            if path_meta:
                stop_index = path_meta["stop_index"]
                stop_entries.sort(
                    key=lambda item: (
                        stop_index.get(item["stopid"], {}).get("seq", 10**9),
                        item["stopid"],
                    )
                )
                path_name = path_meta["name"]
            else:
                path_name = f"Path {pathid}"
                stop_entries.sort(key=lambda item: item["stopid"])

            response_paths.append(
                {
                    "pathid": pathid,
                    "name": path_name,
                    "stops": stop_entries,
                }
            )

        return {
            "routeid": routeid,
            "updated_at": updated_at,
            "paths": response_paths,
        }

    def _get_cached(self, routeid: str, *, allow_expired: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(routeid)
            if entry is None:
                return None
            if not allow_expired and entry.expires_at < now:
                return None
            return entry.snapshot

    def _set_cached(self, routeid: str, snapshot: dict[str, Any]) -> None:
        with self._cache_lock:
            self._cache[routeid] = CacheEntry(
                snapshot=snapshot,
                expires_at=time.time() + self.settings.realtime_cache_ttl,
            )


class RouteBusesService:
    def __init__(self, settings: Settings, client: TDXClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[str, BusesCacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._tracked_routes: dict[str, dict[str, float]] = {}
        self._tracked_routes_lock = threading.Lock()
        self._city_refresh_locks: dict[str, threading.Lock] = {}
        self._city_refresh_locks_guard = threading.Lock()

    def get_buses(self, routeid: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        if not self._route_exists(routeid):
            raise RouteNotFoundError(routeid)

        city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if city is None:
            return self._get_single_route_buses(routeid, force_refresh=force_refresh)

        self._track_route(city, routeid)

        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        city_lock = self._get_city_refresh_lock(city)
        with city_lock:
            if not force_refresh:
                cached = self._get_cached(routeid)
                if cached is not None:
                    return cached

            try:
                self._refresh_city_cache(city, routeid, force_refresh=force_refresh)
            except Exception:
                stale = self._get_cached(routeid, allow_expired=True)
                if stale is not None:
                    LOGGER.warning(
                        "using stale buses cache routeid=%s city=%s after refresh failure",
                        routeid,
                        city,
                    )
                    self._set_cached(routeid, stale)
                    return stale
                raise

            cached = self._get_cached(routeid, allow_expired=True)
            if cached is not None:
                self._set_cached(routeid, cached)
                return cached

        return []

    def _refresh_city_cache(
        self,
        city: str,
        requested_routeid: str,
        *,
        force_refresh: bool,
    ) -> None:
        routeids = self._get_tracked_routeids(city, include_routeid=requested_routeid)
        effective_routeids = sorted(routeids)
        stale_buses = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_buses", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if not force_refresh and all(buses is not None for buses in stale_buses.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_realtime_by_frequency_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if force_refresh or previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "realtime buses batch not modified city=%s routes=%s requested_routeid=%s",
                city,
                len(effective_routeids),
                requested_routeid,
            )
            for routeid, buses in stale_buses.items():
                if buses is not None:
                    self._set_cached(routeid, buses)
            return

        LOGGER.info(
            "realtime buses batch refreshed city=%s routes=%s requested_routeid=%s status=%s items=%s",
            city,
            len(effective_routeids),
            requested_routeid,
            response.status_code,
            len(response.payload or []),
        )

        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_routeid_to_local(
                city,
                item.get("SubRouteUID") or item.get("RouteUID"),
            )
            if routeid in stale_buses:
                items_by_route[routeid].append(item)

        for routeid in effective_routeids:
            self._set_cached(routeid, _build_buses_payload(items_by_route.get(routeid, [])))

    def _get_single_route_buses(self, routeid: str, *, force_refresh: bool) -> list[dict[str, Any]]:
        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        try:
            items: list[dict[str, Any]] = []
            for city in self._candidate_cities_for_route(routeid):
                current_items = self.client.fetch_realtime_by_frequency(city, routeid)
                if current_items:
                    items = current_items
                    break
            buses = _build_buses_payload(items)
        except Exception:
            stale = self._get_cached(routeid, allow_expired=True)
            if stale is not None:
                LOGGER.warning("using stale buses cache routeid=%s after single-route refresh failure", routeid)
                self._set_cached(routeid, stale)
                return stale
            raise

        self._set_cached(routeid, buses)
        return buses

    def _route_exists(self, routeid: str) -> bool:
        with get_connection(self.settings.db_path) as connection:
            return route_exists(connection, routeid)

    def _candidate_cities_for_route(self, routeid: str) -> list[str]:
        candidate_cities: list[str] = []
        guessed_city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if guessed_city:
            if guessed_city == INTERCITY_CITY_NAME:
                return [guessed_city]
            candidate_cities.append(guessed_city)
        for city in self.settings.tdx_cities:
            if city not in candidate_cities:
                candidate_cities.append(city)
        return candidate_cities

    def _track_route(self, city: str, routeid: str) -> None:
        self._get_tracked_routeids(city, include_routeid=routeid)

    def _get_tracked_routeids(self, city: str, *, include_routeid: str | None = None) -> list[str]:
        now = time.monotonic()
        expires_at = now + self.settings.realtime_track_ttl

        with self._tracked_routes_lock:
            city_routes = self._tracked_routes.setdefault(city, {})
            expired_routeids = [routeid for routeid, route_expires_at in city_routes.items() if route_expires_at < now]
            for routeid in expired_routeids:
                city_routes.pop(routeid, None)

            if include_routeid:
                city_routes[include_routeid] = expires_at

            if not city_routes:
                self._tracked_routes.pop(city, None)
                return []

            return sorted(city_routes)

    def _get_city_refresh_lock(self, city: str) -> threading.Lock:
        with self._city_refresh_locks_guard:
            return self._city_refresh_locks.setdefault(city, threading.Lock())

    def _get_cached(self, routeid: str, *, allow_expired: bool = False) -> list[dict[str, Any]] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(routeid)
            if entry is None:
                return None
            if not allow_expired and entry.expires_at < now:
                return None
            return [dict(item) for item in entry.buses]

    def _set_cached(self, routeid: str, buses: list[dict[str, Any]]) -> None:
        with self._cache_lock:
            self._cache[routeid] = BusesCacheEntry(
                buses=[dict(item) for item in buses],
                expires_at=time.time() + self.settings.realtime_cache_ttl,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a realtime route snapshot from TDX.")
    parser.add_argument("--routeid", required=True, help="TDX SubRouteUID, for example TPE307.")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.project_dir)
    settings.require_tdx_credentials()
    init_db(settings.db_path)

    token_manager = TDXTokenManager(settings)
    client = TDXClient(settings, token_manager)
    service = RealtimeService(settings, client)

    try:
        snapshot = service.get_snapshot(args.routeid, force_refresh=True)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    finally:
        client.close()
        token_manager.close()
        shutdown_logging()


if __name__ == "__main__":
    main()
