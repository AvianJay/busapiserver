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
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}


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


def _build_message(item: dict[str, Any]) -> str:
    stop_status = item.get("StopStatus")
    if stop_status in STOP_STATUS_MESSAGES:
        return STOP_STATUS_MESSAGES[stop_status]

    estimate_time = item.get("EstimateTime")
    if estimate_time is not None:
        estimate_time = int(estimate_time)
        if estimate_time <= 30:
            return "即將進站"
        return f"{max(1, math.ceil(estimate_time / 60))} 分"

    if item.get("IsLastBus"):
        return "末班車"

    scheduled_time = (item.get("ScheduledTime") or "").strip()
    if scheduled_time:
        return scheduled_time

    return ""


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
            current_items = payload.get("Items") or []
            if current_items:
                wrapper = payload
                items = current_items
                break

        wrapper = wrapper or {"Items": [], "UpdateTime": None}
        if not items:
            items = wrapper.get("Items") or []

        updated_at = (
            _to_unix_seconds(wrapper.get("UpdateTime"))
            or _to_unix_seconds(wrapper.get("SrcUpdateTime"))
            or int(time.time())
        )

        paths = static_route["paths"]
        grouped: dict[int, dict[str, dict[str, Any]]] = {}

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
                    "time": updated_at,
                    "buses": [],
                },
            )

            estimate_time = item.get("EstimateTime")
            if estimate_time is not None:
                estimate_time = int(estimate_time)
                if stop_bucket["eta"] is None or estimate_time < stop_bucket["eta"]:
                    stop_bucket["eta"] = estimate_time
                    stop_bucket["message"] = _build_message(item)

            if not stop_bucket["message"]:
                stop_bucket["message"] = _build_message(item)

            stop_time = (
                _to_unix_seconds(item.get("DataTime"))
                or _to_unix_seconds(item.get("RecTime"))
                or _to_unix_seconds(item.get("TransTime"))
                or updated_at
            )
            stop_bucket["time"] = max(stop_bucket["time"], stop_time)

            plate = (item.get("PlateNumb") or "").strip()
            if plate and plate != "-1":
                if all(bus["id"] != plate for bus in stop_bucket["buses"]):
                    stop_bucket["buses"].append({"id": plate, "type": "normal"})

        response_paths = []
        seen_pathids = set(paths)
        seen_pathids.update(grouped)

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
