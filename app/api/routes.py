from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
from datetime import datetime, timezone
import threading
import time

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.db import get_connection, load_database_version, load_path_points, load_route_static, path_exists
from app.config import guess_city_from_routeid
from app.sync_realtime import RouteNotFoundError


router = APIRouter()

RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limit_lock = threading.Lock()
_rate_limit_hits: MutableMapping[tuple[str, str], deque[float]] = {}


def _get_client_ip(request: Request) -> str:
    # Trust Cloudflare's connecting IP first.
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    return (request.client.host if request.client else "unknown").strip() or "unknown"


def _check_route_rate_limit(request: Request) -> None:
    route = request.scope.get("route")
    route_template = getattr(route, "path", request.url.path)
    if not isinstance(route_template, str) or not route_template.startswith("/api/v1/routes/"):
        return

    ip = _get_client_ip(request)
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    key = (ip, route_template)

    with _rate_limit_lock:
        hits = _rate_limit_hits.setdefault(key, deque())
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, int(hits[0] + RATE_LIMIT_WINDOW_SECONDS - now))
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Limit is 30 requests per minute per endpoint.",
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)


def _encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    output: list[str] = []
    previous_lat = 0
    previous_lon = 0

    for lat, lon in points:
        lat_value = int(round(lat * factor))
        lon_value = int(round(lon * factor))
        for value in (lat_value - previous_lat, lon_value - previous_lon):
            encoded = value << 1
            if value < 0:
                encoded = ~encoded
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
        previous_lat = lat_value
        previous_lon = lon_value

    return "".join(output)


def _to_unix_seconds(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_cities_for_route(routeid: str, configured_cities: tuple[str, ...]) -> list[str]:
    cities: list[str] = []
    guessed_city = guess_city_from_routeid(routeid, configured_cities)
    if guessed_city:
        cities.append(guessed_city)
    for city in configured_cities:
        if city not in cities:
            cities.append(city)
    return cities


@router.get("/downloads/bus.db")
def download_bus_db(request: Request) -> FileResponse:
    db_path = request.app.state.settings.download_db_path
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="bus.db does not exist yet.")
    return FileResponse(db_path, filename="bus.db")


@router.get("/api/v1/routes/{routeid}/realtime")
def get_route_realtime(routeid: str, request: Request) -> dict:
    _check_route_rate_limit(request)
    service = request.app.state.realtime_service
    try:
        return service.get_snapshot(routeid)
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc


@router.get("/api/v1/routes/{routeid}/realtime/buses")
def get_route_buses(routeid: str, request: Request) -> list[dict]:
    _check_route_rate_limit(request)
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        static_route = load_route_static(connection, routeid)
    if static_route is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")

    client = request.app.state.tdx_client
    items: list[dict] = []
    try:
        for city in _candidate_cities_for_route(routeid, settings.tdx_cities):
            current_items = client.fetch_realtime_by_frequency(city, routeid)
            if current_items:
                items = current_items
                break
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc

    buses_by_plate: dict[str, dict] = {}
    for item in items:
        if _to_int_or_none(item.get("DutyStatus")) == 2:
            continue

        plate = (item.get("PlateNumb") or "").strip()
        if not plate or plate == "-1":
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


@router.get("/api/v1/routes/{routeid}/paths/{pathid}/points")
def get_route_path_points(routeid: str, pathid: int, request: Request) -> dict:
    _check_route_rate_limit(request)
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        if not path_exists(connection, routeid, pathid):
            raise HTTPException(
                status_code=404,
                detail=f"Path {pathid} for route {routeid} was not found.",
            )
        points = load_path_points(connection, routeid, pathid)

    return {
        "routeid": routeid,
        "pathid": pathid,
        "polyline": _encode_polyline(points),
    }


@router.get("/api/v1/routes/{routeid}/stops")
def get_route_stops(routeid: str, request: Request) -> dict:
    _check_route_rate_limit(request)
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        static_route = load_route_static(connection, routeid)
    if static_route is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")

    response_paths = []
    for pathid in sorted(static_route["paths"]):
        path = static_route["paths"][pathid]
        response_paths.append(
            {
                "pathid": pathid,
                "name": path["name"],
                "stops": list(path["stops"]),
            }
        )

    return {
        "routeid": routeid,
        "name": static_route["name"],
        "paths": response_paths,
    }


@router.get("/api/v1/database/{name}/version")
def get_database_version(name: str, request: Request) -> dict:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        version = load_database_version(connection, name)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Database version for {name} was not found.")
    return version
