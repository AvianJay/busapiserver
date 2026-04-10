from __future__ import annotations

import argparse
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings, get_settings, guess_city_from_routeid
from app.db import get_connection, init_db, load_route_static
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient


STOP_STATUS_MESSAGES = {
    1: "\u5c1a\u672a\u767c\u8eca",
    2: "\u4ea4\u7ba1\u4e0d\u505c\u9760",
    3: "\u672b\u73ed\u8eca\u5df2\u904e",
    4: "\u4eca\u65e5\u672a\u71df\u904b",
}

ARRIVING_VEHICLE_STOP_STATUS = 1


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


def _build_message(item: dict[str, Any]) -> str:
    stop_status = item.get("StopStatus")

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

    estimate_time = item.get("EstimateTime")
    if estimate_time is not None:
        estimate_time = int(estimate_time)
        # if estimate_time <= 30:
        #     return "進站中"
        # return f"{max(1, math.ceil(estimate_time / 60))} \u5206"
        return ""

    if item.get("IsLastBus"):
        return "\u672b\u73ed\u8eca"

    scheduled_time = (item.get("ScheduledTime") or "").strip()
    if scheduled_time:
        return scheduled_time

    return ""


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_plate(value: Any) -> str | None:
    if value is None:
        return None
    plate = str(value).strip()
    if not plate or plate == "-1":
        return None
    return plate


def _build_eta_text(eta: int | None, stop_status: int | None) -> str:
    if stop_status in STOP_STATUS_MESSAGES:
        return STOP_STATUS_MESSAGES[stop_status]
    if eta is None:
        return ""
    if eta <= 30:
        return "進站中"
    return f"{max(1, math.ceil(eta / 60))} 分"


def _append_stop_eta(
    stop_bucket: dict[str, Any],
    *,
    plate: str | None,
    eta: int | None,
    message: str,
    is_arriving: bool,
) -> None:
    if plate is None and eta is None and not message:
        return
    stop_bucket.setdefault("etas", []).append(
        {
            "plate": plate,
            "eta": eta,
            "message": message,
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
        message = str(entry.get("message") or "").strip()
        is_arriving = bool(entry.get("is_arriving"))
        if plate is None and eta is None and not message:
            continue

        key = plate or f"anon:{eta}:{message}"
        candidate = {
            "plate": plate,
            "eta": eta,
            "message": message,
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
        first_eta = next(
            (
                entry.get("eta")
                for entry in stop_bucket["etas"]
                if entry.get("eta") is not None
            ),
            None,
        )
        stop_bucket["eta"] = first_eta
    if not stop_bucket.get("message"):
        first_message = next(
            (
                str(entry.get("message") or "").strip()
                for entry in stop_bucket["etas"]
                if str(entry.get("message") or "").strip()
            ),
            "",
        )
        stop_bucket["message"] = first_message


def _collect_plate_observations(item: dict[str, Any], *, pathid: int, stopid: str) -> list[PlateObservation]:
    observations: list[PlateObservation] = []

    top_plate = (item.get("PlateNumb") or "").strip()
    top_is_arriving = item.get("VehicleStopStatus") == ARRIVING_VEHICLE_STOP_STATUS
    if top_plate and top_plate != "-1" and top_is_arriving:
        observations.append(
            PlateObservation(
                plate=top_plate,
                pathid=pathid,
                stopid=stopid,
                eta=_to_int_or_none(item.get("EstimateTime")),
                is_arriving=top_is_arriving,
            )
        )

    for estimate in item.get("Estimates") or []:
        estimate_plate = (estimate.get("PlateNumb") or "").strip()
        if not estimate_plate or estimate_plate == "-1":
            continue
        estimate_is_arriving = estimate.get("VehicleStopStatus") == ARRIVING_VEHICLE_STOP_STATUS
        # N1 Estimates is often a list of multiple upcoming buses for a stop.
        # Only trust estimate plates when API explicitly marks "arriving".
        if not estimate_is_arriving:
            continue
        observations.append(
            PlateObservation(
                plate=estimate_plate,
                pathid=pathid,
                stopid=stopid,
                eta=_to_int_or_none(estimate.get("EstimateTime")),
                is_arriving=estimate_is_arriving,
            )
        )

    return observations


class RealtimeService:
    def __init__(self, settings: Settings, client: TDXClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get_snapshot(self, routeid: str, *, force_refresh: bool = False) -> dict[str, Any]:
        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        try:
            snapshot = self.fetch_snapshot(routeid)
        except Exception:
            cached = self._get_cached(routeid, allow_expired=True)
            if cached is not None:
                return cached
            raise

        self._set_cached(routeid, snapshot)
        return snapshot

    def fetch_snapshot(self, routeid: str) -> dict[str, Any]:
        with get_connection(self.settings.db_path) as connection:
            static_route = load_route_static(connection, routeid)
        if static_route is None:
            raise RouteNotFoundError(routeid)

        candidate_cities = []
        guessed_city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if guessed_city:
            candidate_cities.append(guessed_city)
        for city in self.settings.tdx_cities:
            if city not in candidate_cities:
                candidate_cities.append(city)

        wrapper = None
        items = []
        for city in candidate_cities:
            payload = self.client.fetch_estimated_time_of_arrival(city, routeid)
            if wrapper is None:
                wrapper = payload
            current_items = payload or []
            if current_items:
                wrapper = payload
                items = current_items
                break

        wrapper = wrapper or []
        if not items:
            items = wrapper or []

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
                    # "time": updated_at,
                    "buses": [],
                    "etas": [],
                },
            )

            estimate_time = _to_int_or_none(item.get("EstimateTime"))
            message = _build_message(item)
            if estimate_time is not None:
                if stop_bucket["eta"] is None or estimate_time < stop_bucket["eta"]:
                    stop_bucket["eta"] = estimate_time
                    stop_bucket["message"] = message

            if not stop_bucket["message"]:
                stop_bucket["message"] = message

            top_plate = _normalize_plate(item.get("PlateNumb"))
            top_is_arriving = item.get("VehicleStopStatus") == ARRIVING_VEHICLE_STOP_STATUS
            _append_stop_eta(
                stop_bucket,
                plate=top_plate,
                eta=estimate_time,
                message=message,
                is_arriving=top_is_arriving,
            )

            for estimate in item.get("Estimates") or []:
                estimate_plate = _normalize_plate(estimate.get("PlateNumb"))
                estimate_eta = _to_int_or_none(estimate.get("EstimateTime"))
                estimate_status = _to_int_or_none(estimate.get("StopStatus"))
                estimate_is_arriving = (
                    estimate.get("VehicleStopStatus") == ARRIVING_VEHICLE_STOP_STATUS
                )
                _append_stop_eta(
                    stop_bucket,
                    plate=estimate_plate,
                    eta=estimate_eta,
                    message=_build_eta_text(estimate_eta, estimate_status),
                    is_arriving=estimate_is_arriving,
                )

            stop_time = (
                _to_unix_seconds(item.get("UpdateTime"))
                or _to_unix_seconds(item.get("DataTime"))
                or _to_unix_seconds(item.get("SrcRecTime"))
                or _to_unix_seconds(item.get("SrcTransTime"))
                or _to_unix_seconds(item.get("TransTime"))
                or _to_unix_seconds(item.get("SrcUpdateTime"))
                or updated_at
            )
            # stop_bucket["time"] = max(stop_bucket["time"], stop_time)

            for observation in _collect_plate_observations(item, pathid=pathid, stopid=stopid):
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
        with self._lock:
            entry = self._cache.get(routeid)
            if entry is None:
                return None
            if not allow_expired and entry.expires_at < now:
                return None
            return entry.snapshot

    def _set_cached(self, routeid: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._cache[routeid] = CacheEntry(
                snapshot=snapshot,
                expires_at=time.time() + self.settings.realtime_cache_ttl,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a realtime route snapshot from TDX.")
    parser.add_argument("--routeid", required=True, help="TDX SubRouteUID, for example TPE307.")
    args = parser.parse_args()

    settings = get_settings()
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


if __name__ == "__main__":
    main()
