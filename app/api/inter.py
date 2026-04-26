from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query, Request

from app.db import (
    get_connection,
    inter_path_exists,
    inter_route_exists,
    load_inter_path_points,
    load_inter_route_static,
)
from app.logging_utils import get_logger

LOGGER = get_logger("inter")

router = APIRouter(prefix="/api/v1/inter", tags=["inter"])

REALTIME_CACHE_TTL = 5
ALERTS_CACHE_TTL = 600


@dataclass
class _CacheEntry:
    data: Any
    fetched_at: float = field(default_factory=time.monotonic)

    def is_fresh(self, ttl: float) -> bool:
        return (time.monotonic() - self.fetched_at) < ttl


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _get_cached(key: str, ttl: float) -> Any:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry.is_fresh(ttl):
            return entry.data
    return None


def _set_cached(key: str, data: Any) -> None:
    with _cache_lock:
        _cache[key] = _CacheEntry(data=data)


def _get_tdx(request: Request):
    return request.app.state.tdx_client


def _serialize_operator_names(operator_names: str | None) -> list[dict]:
    names = [part.strip() for part in (operator_names or "").split("/")]
    return [{"id": name, "name": name} for name in names if name]


def _route_row_to_response(row) -> dict:
    return {
        "routeid": row["routeid"],
        "route_uid": row["route_uid"],
        "sub_route_uid": row["routeid"],
        "name": row["name"] or row["routeid"],
        "name_en": row["name_en"] or "",
        "departure": row["departure"] or "",
        "destination": row["destination"] or "",
        "operators": _serialize_operator_names(row["operator_names"]),
    }


def _require_inter_route(connection, routeid: str) -> None:
    if not inter_route_exists(connection, routeid):
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")


def _to_unix_seconds(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _stop_status_message(status: int | None) -> str:
    return {
        1: "尚未發車",
        2: "交管不停靠",
        3: "末班車已過",
        4: "今日未營運",
    }.get(status or 0, "")


def _fetch_eta(tdx, route_uid: str) -> list[dict]:
    cache_key = f"inter_eta_{route_uid}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    escaped = route_uid.replace("'", "''")
    payload = tdx._request_json(  # noqa: SLF001
        "/v2/Bus/EstimatedTimeOfArrival/InterCity",
        params={
            "$filter": f"RouteUID eq '{escaped}' or SubRouteUID eq '{escaped}'",
            "$format": "JSON",
            "$top": 5000,
        },
    )
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = list(payload.get("Items") or [])
    else:
        items = []
    _set_cached(cache_key, items)
    return items


def _fetch_realtime_buses(tdx, route_uid: str) -> list[dict]:
    cache_key = f"inter_buses_{route_uid}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    escaped = route_uid.replace("'", "''")
    payload = tdx._request_json(  # noqa: SLF001
        "/v2/Bus/RealTimeByFrequency/InterCity",
        params={
            "$filter": f"RouteUID eq '{escaped}' or SubRouteUID eq '{escaped}'",
            "$format": "JSON",
            "$top": 1000,
        },
    )
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = list(payload.get("Items") or [])
    else:
        items = []
    _set_cached(cache_key, items)
    return items


def _fetch_alerts(tdx) -> list[dict]:
    cache_key = "inter_alerts"
    cached = _get_cached(cache_key, ALERTS_CACHE_TTL)
    if cached is not None:
        return cached

    raw = tdx.fetch_paginated_items("/v2/Bus/Alert/InterCity")
    _set_cached(cache_key, raw)
    return raw


def _build_realtime_snapshot(routeid: str, static_route: dict, eta_items: list[dict]) -> dict:
    now_ts = int(time.time())
    path_stop_lookup: dict[int, dict[str, dict]] = {}
    response_paths: list[dict] = []

    for pathid in sorted(static_route["paths"]):
        path = static_route["paths"][pathid]
        stop_map: dict[str, dict] = {}
        response_stops: list[dict] = []
        for stop in path["stops"]:
            entry = {
                "seq": stop["seq"],
                "stopid": stop["stopid"],
                "name": stop["name"],
                "name_en": stop["name_en"],
                "lat": stop["lat"],
                "lon": stop["lon"],
                "eta": None,
                "message": "",
                "buses": [],
            }
            stop_map[stop["stopid"]] = entry
            response_stops.append(entry)

        path_stop_lookup[pathid] = stop_map
        response_paths.append(
            {
                "pathid": pathid,
                "name": path["name"],
                "stops": response_stops,
            }
        )

    item_times: list[int] = []
    for item in eta_items:
        update = (
            _to_unix_seconds(item.get("UpdateTime"))
            or _to_unix_seconds(item.get("DataTime"))
            or _to_unix_seconds(item.get("SrcUpdateTime"))
        )
        if update is not None:
            item_times.append(update)

        direction = int(item.get("Direction") or 0)
        stopid = item.get("StopUID") or item.get("StopID")
        if not stopid:
            continue
        stop_map = path_stop_lookup.get(direction)
        if stop_map is None:
            continue
        stop = stop_map.get(stopid)
        if stop is None:
            continue

        estimate = item.get("EstimateTime")
        status = item.get("StopStatus")
        try:
            estimate_int = int(estimate) if estimate is not None else None
        except (TypeError, ValueError):
            estimate_int = None
        try:
            status_int = int(status) if status is not None else 0
        except (TypeError, ValueError):
            status_int = 0

        if status_int in {1, 2, 3, 4}:
            stop["message"] = _stop_status_message(status_int)
            stop["eta"] = None
        elif estimate_int is not None:
            if stop["eta"] is None or estimate_int < stop["eta"]:
                stop["eta"] = estimate_int
                stop["message"] = ""

        plate = (item.get("PlateNumb") or "").strip()
        if plate and plate != "-1" and all(bus["id"] != plate for bus in stop["buses"]):
            stop["buses"].append({"id": plate, "type": "normal"})

    return {
        "routeid": routeid,
        "updated_at": max(item_times) if item_times else now_ts,
        "paths": response_paths,
    }


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


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6378137.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@router.get("/routes/search")
async def search_routes(
    request: Request,
    q: str = Query("", max_length=120, description="Search query."),
    limit: int = Query(80, ge=1, le=200),
):
    normalized_query = q.strip()
    params: list[object] = []
    where_clause = ""
    if normalized_query:
        wildcard = f"%{normalized_query}%"
        where_clause = """
            WHERE (
                inter_routes.name LIKE ?
                OR COALESCE(inter_routes.name_en, '') LIKE ?
                OR inter_routes.routeid LIKE ?
                OR COALESCE(inter_routes.departure, '') LIKE ?
                OR COALESCE(inter_routes.destination, '') LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM inter_paths p
                    WHERE p.routeid = inter_routes.routeid
                      AND p.name LIKE ?
                )
            )
        """
        params.extend([wildcard, wildcard, wildcard, wildcard, wildcard, wildcard])
    params.append(limit)

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                inter_routes.routeid,
                inter_routes.route_uid,
                inter_routes.name,
                inter_routes.name_en,
                inter_routes.departure,
                inter_routes.destination,
                inter_routes.operator_names
            FROM inter_routes
            {where_clause}
            ORDER BY inter_routes.name ASC, inter_routes.routeid ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    return [_route_row_to_response(row) for row in rows]


@router.get("/routes/{routeid}")
async def get_route_detail(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        row = connection.execute(
            """
            SELECT routeid, route_uid, name, name_en, departure, destination, operator_names
            FROM inter_routes
            WHERE routeid = ?
            LIMIT 1
            """,
            (routeid,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")
    return _route_row_to_response(row)


@router.get("/routes/{routeid}/stops")
async def get_route_stops(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        static_route = load_inter_route_static(connection, routeid)
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


@router.get("/routes/{routeid}/realtime")
async def get_route_realtime(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        static_route = load_inter_route_static(connection, routeid)
    if static_route is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")

    tdx = _get_tdx(request)
    try:
        eta_items = _fetch_eta(tdx, routeid)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _build_realtime_snapshot(routeid, static_route, eta_items)


@router.get("/routes/{routeid}/realtime/buses")
async def get_route_buses(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        _require_inter_route(connection, routeid)

    tdx = _get_tdx(request)
    try:
        items = _fetch_realtime_buses(tdx, routeid)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    buses = []
    for item in items:
        pos = item.get("BusPosition") or {}
        plate = (item.get("PlateNumb") or "").strip()
        if not plate or plate == "-1":
            continue
        buses.append(
            {
                "id": plate,
                "lat": pos.get("PositionLat", 0),
                "lon": pos.get("PositionLon", 0),
                "speed": item.get("Speed", 0),
                "azimuth": item.get("Azimuth", 0),
                "direction": int(item.get("Direction") or 0),
                "duty_status": item.get("DutyStatus", 0),
                "bus_status": item.get("BusStatus", 0),
            }
        )
    return buses


@router.get("/routes/{routeid}/path")
async def get_route_path(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        _require_inter_route(connection, routeid)

        rows = connection.execute(
            """
            SELECT pathid, name
            FROM inter_paths
            WHERE routeid = ?
            ORDER BY pathid
            """,
            (routeid,),
        ).fetchall()

        paths = []
        for row in rows:
            pathid = int(row["pathid"])
            if not inter_path_exists(connection, routeid, pathid):
                continue
            points = load_inter_path_points(connection, routeid, pathid)
            paths.append(
                {
                    "pathid": pathid,
                    "name": row["name"],
                    "polyline": _encode_polyline(points),
                    "point_count": len(points),
                }
            )

    return {"routeid": routeid, "paths": paths}


@router.get("/routes/{routeid}/operators")
async def get_route_operators(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        _require_inter_route(connection, routeid)
        rows = connection.execute(
            """
            SELECT o.operator_id, o.name, o.name_en, o.code, o.phone, o.email, o.url
            FROM inter_route_operators ro
            JOIN operators o ON o.operator_id = ro.operator_id
            WHERE ro.routeid = ?
            ORDER BY ro.seq, o.name
            """,
            (routeid,),
        ).fetchall()

    return [
        {
            "operator_id": row["operator_id"],
            "name": row["name"],
            "name_en": row["name_en"],
            "code": row["code"],
            "phone": row["phone"],
            "email": row["email"],
            "url": row["url"],
        }
        for row in rows
    ]


@router.get("/routes/{routeid}/schedule")
async def get_route_schedule(routeid: str, request: Request):
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        _require_inter_route(connection, routeid)
        rows = connection.execute(
            """
            SELECT subroute_uid, direction, kind, seq, service_days, payload
            FROM inter_route_schedules
            WHERE routeid = ?
            ORDER BY subroute_uid, direction, kind, seq
            """,
            (routeid,),
        ).fetchall()

    return [
        {
            "subroute_uid": row["subroute_uid"],
            "direction": row["direction"],
            "kind": row["kind"],
            "seq": row["seq"],
            "service_days": json.loads(row["service_days"]),
            "payload": json.loads(row["payload"]),
        }
        for row in rows
    ]


@router.get("/alerts")
async def get_alerts(request: Request):
    tdx = _get_tdx(request)
    try:
        raw = _fetch_alerts(tdx)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [
        {
            "alert_id": item.get("AlertID", ""),
            "title": item.get("Title", ""),
            "description": item.get("Description", ""),
            "level": item.get("Level", 0),
            "start": _to_unix_seconds(item.get("StartTime")),
            "end": _to_unix_seconds(item.get("EndTime")),
            "publish": _to_unix_seconds(item.get("PublishTime")),
            "url": item.get("Url", ""),
        }
        for item in raw
    ]


@router.get("/nearby")
async def get_nearby(
    request: Request,
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius: int = Query(800, ge=100, le=5000, description="Search radius in meters"),
    limit: int = Query(30, ge=1, le=100),
):
    lat_delta = radius / 111320
    lon_delta = radius / max(1.0, abs(111320 * math.cos(math.radians(lat))))

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                s.routeid,
                s.pathid,
                s.stopid,
                s.name AS stop_name,
                s.name_en AS stop_name_en,
                s.seq,
                s.lat,
                s.lon,
                r.route_uid,
                r.name,
                r.name_en,
                r.departure,
                r.destination,
                r.operator_names
            FROM inter_stops s
            JOIN inter_routes r ON r.routeid = s.routeid
            WHERE ABS(s.lat - ?) <= ?
              AND ABS(s.lon - ?) <= ?
            ORDER BY s.routeid ASC, s.pathid ASC, s.seq ASC
            """,
            (lat, lat_delta, lon, lon_delta),
        ).fetchall()

    results = []
    seen: set[tuple[str, int, str]] = set()
    for row in rows:
        distance = _haversine_meters(lat, lon, float(row["lat"]), float(row["lon"]))
        if distance > radius:
            continue

        dedupe_key = (row["routeid"], int(row["pathid"]), row["stopid"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        results.append(
            {
                "route": _route_row_to_response(row),
                "stop": {
                    "pathid": int(row["pathid"]),
                    "stopid": row["stopid"],
                    "name": row["stop_name"],
                    "name_en": row["stop_name_en"],
                    "seq": int(row["seq"]),
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                },
                "distance_meters": round(distance),
            }
        )

    results.sort(
        key=lambda item: (
            item.get("distance_meters", 10**9),
            (item.get("route") or {}).get("name", ""),
            ((item.get("stop") or {}).get("seq") or 0),
        )
    )
    return results[:limit]
