from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
import re
import threading
import time

import requests
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.db import get_connection, load_database_version, load_path_points, load_route_static, path_exists
from app.config import CITY_NAME_TO_PREFIX, CITY_PREFIX_TO_NAME
from app.sync_realtime import RouteNotFoundError


router = APIRouter()

RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limit_lock = threading.Lock()
_rate_limit_hits: MutableMapping[tuple[str, str], deque[float]] = {}
_download_name_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


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

    if "realtime" in route_template:
        route_template = "/api/v1/routes/realtime"

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
                detail="Too many requests. Please try again later.",
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


_city_name_to_prefix_lower = {
    city.lower(): prefix for city, prefix in CITY_NAME_TO_PREFIX.items()
}
_city_name_canonical_lower = {city.lower(): city for city in CITY_NAME_TO_PREFIX}


def _resolve_city(city: str) -> tuple[str, str] | None:
    normalized = city.strip()
    if not normalized:
        return None

    if len(normalized) == 3 and normalized.isalpha():
        prefix = normalized.upper()
        city_name = CITY_PREFIX_TO_NAME.get(prefix)
        if city_name is None:
            return None
        return prefix, city_name

    city_name = _city_name_canonical_lower.get(normalized.lower())
    if city_name is None:
        return None
    prefix = _city_name_to_prefix_lower.get(city_name.lower())
    if prefix is None:
        return None
    return prefix, city_name


@router.get("/downloads/bus.db")
def download_bus_db(request: Request) -> FileResponse:
    db_path = request.app.state.settings.download_db_path
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="bus.db does not exist yet.")
    return FileResponse(db_path, filename="bus.db")


@router.get("/downloads/{name}.db")
def download_city_db(name: str, request: Request) -> FileResponse:
    if not _download_name_pattern.fullmatch(name):
        raise HTTPException(status_code=404, detail="Invalid download database name.")

    db_path = request.app.state.settings.download_db_path.parent / f"{name}.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"{name}.db does not exist yet.")
    return FileResponse(db_path, filename=f"{name}.db")


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


@router.get("/api/v1/cities/{city}/routes")
def search_city_routes(
    city: str,
    request: Request,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=80, ge=1, le=200),
) -> list[dict]:
    resolved_city = _resolve_city(city)
    if resolved_city is None:
        raise HTTPException(status_code=404, detail=f"City {city} was not found.")
    prefix, city_name = resolved_city

    normalized_query = query.strip()
    where_clause = ""
    query_args: list[object] = [prefix]
    if normalized_query:
        where_clause = """
            AND (
                routes.name LIKE ?
                OR routes.routeid LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM paths p
                    WHERE p.routeid = routes.routeid
                      AND p.name LIKE ?
                )
            )
        """
        wildcard = f"%{normalized_query}%"
        query_args.extend([wildcard, wildcard, wildcard])

    query_args.append(limit)

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                routes.routeid AS routeid,
                routes.name AS route_name,
                routes.name_en AS route_name_en,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name, ' / ')
                        FROM (
                            SELECT DISTINCT p.name AS path_name
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name_en, ' / ')
                        FROM (
                            SELECT DISTINCT p.name_en AS path_name_en
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name_en, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name_en,
                COALESCE(
                    (
                        SELECT p.pathid
                        FROM paths p
                        WHERE p.routeid = routes.routeid
                        ORDER BY p.pathid ASC
                        LIMIT 1
                    ),
                    0
                ) AS pathid,
                SUBSTR(routes.routeid, 1, 3) AS city_code
            FROM routes
            WHERE routes.routeid LIKE ?
            {where_clause}
            ORDER BY routes.routeid ASC
            LIMIT ?
            """,
            tuple(query_args),
        ).fetchall()

    return [
        {
            "routeid": row["routeid"],
            "route_name": row["route_name"],
            "route_name_en": row["route_name_en"],
            "pathid": int(row["pathid"]),
            "path_name": row["path_name"],
            "path_name_en": row["path_name_en"],
            "city_code": row["city_code"],
        }
        for row in rows
    ]


@router.get("/api/v1/routes/{routeid}/realtime/buses")
def get_route_buses(routeid: str, request: Request) -> list[dict]:
    _check_route_rate_limit(request)
    service = request.app.state.route_buses_service
    try:
        return service.get_buses(routeid)
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc


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
