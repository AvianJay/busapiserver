from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
import threading
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.db import get_connection, load_database_version, load_path_points, load_route_static, path_exists, route_exists
from app.config import CITY_NAME_TO_PREFIX, CITY_PREFIX_TO_NAME, guess_city_from_routeid
from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit
from app.sync_realtime import RouteNotFoundError

LOGGER = get_logger("routes")


router = APIRouter(tags=["Bus"], dependencies=[Depends(enforce_rate_limit)])

_download_name_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

ALERTS_CACHE_TTL_SECONDS = 600  # 10 minutes


@dataclass
class _AlertsCacheEntry:
    alerts: list[dict]
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < ALERTS_CACHE_TTL_SECONDS


_alerts_cache: dict[str, _AlertsCacheEntry] = {}
_alerts_cache_lock = threading.Lock()
_alerts_in_flight: dict[str, list[dict] | None] = {}
_alerts_in_flight_lock = threading.Lock()


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


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6378137.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius * c


def _search_routes(
    connection,
    *,
    routeid_like: str | None,
    query: str,
    limit: int,
) -> list[dict]:
    normalized_query = query.strip()
    prefix_clause = ""
    query_args: list[object] = []
    if routeid_like:
        prefix_clause = "AND routes.routeid LIKE ?"
        query_args.append(routeid_like)

    where_clause = ""
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

    order_clause = "ORDER BY routes.routeid ASC"
    if normalized_query:
        normalized_lower = normalized_query.lower()
        query_args.extend(
            [
                normalized_lower,
                f"{normalized_lower}%",
                f"%{normalized_lower}%",
                normalized_lower,
                f"%{normalized_lower}%",
                len(normalized_query),
            ]
        )
        order_clause = """
        ORDER BY
            CASE
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) = ? THEN 0
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) LIKE ? THEN 1
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) LIKE ? THEN 2
                WHEN LOWER(TRIM(COALESCE(routes.routeid, ''))) = ? THEN 3
                WHEN LOWER(TRIM(COALESCE(routes.routeid, ''))) LIKE ? THEN 4
                ELSE 5
            END ASC,
            ABS(LENGTH(TRIM(COALESCE(routes.name, ''))) - ?) ASC,
            LENGTH(TRIM(COALESCE(routes.name, ''))) ASC,
            LOWER(TRIM(COALESCE(routes.name, ''))) ASC,
            routes.routeid ASC
        """

    query_args.append(limit)

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
        WHERE 1 = 1
        {prefix_clause}
        {where_clause}
        {order_clause}
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


def _load_nearby_stops(
    connection,
    *,
    routeid_like: str,
    latitude: float,
    longitude: float,
    radius: float,
    limit: int,
) -> list[dict]:
    lat_delta = radius / 111320
    lon_scale = abs(math.cos(math.radians(latitude)))
    lon_delta = 180.0 if lon_scale < 1e-6 else radius / (111320 * lon_scale)
    candidate_limit = max(limit * 25, 200)

    rows = connection.execute(
        """
        SELECT
            s.routeid AS routeid,
            s.pathid AS pathid,
            s.stopid AS stopid,
            s.name AS stop_name,
            s.seq AS seq,
            s.lat AS lat,
            s.lon AS lon,
            r.name AS route_name,
            COALESCE(p.name, '') AS path_name,
            SUBSTR(s.routeid, 1, 3) AS city_code
        FROM stops s
        JOIN routes r ON r.routeid = s.routeid
        LEFT JOIN paths p ON p.routeid = s.routeid AND p.pathid = s.pathid
        WHERE s.routeid LIKE ?
          AND s.lat BETWEEN ? AND ?
          AND s.lon BETWEEN ? AND ?
        ORDER BY s.routeid ASC, s.pathid ASC, s.seq ASC
        LIMIT ?
        """,
        (
            routeid_like,
            latitude - lat_delta,
            latitude + lat_delta,
            longitude - lon_delta,
            longitude + lon_delta,
            candidate_limit,
        ),
    ).fetchall()

    results: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for row in rows:
        row_lat = float(row["lat"])
        row_lon = float(row["lon"])
        distance = _distance_meters(latitude, longitude, row_lat, row_lon)
        if distance > radius:
            continue

        dedupe_key = (row["routeid"], int(row["pathid"]), row["stopid"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        results.append(
            {
                "routeid": row["routeid"],
                "pathid": int(row["pathid"]),
                "stopid": row["stopid"],
                "stop_name": row["stop_name"],
                "seq": int(row["seq"]),
                "lat": row_lat,
                "lon": row_lon,
                "distance": round(distance, 2),
                "route_name": row["route_name"],
                "path_name": row["path_name"],
                "city_code": row["city_code"],
            }
        )

    results.sort(key=lambda item: item["distance"])
    return results[:limit]


@router.get("/")
def root() -> dict:
    # redirect to main app
    return RedirectResponse(url="https://busapp.avianjay.sbs/")


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


_BATCH_ROUTES_REALTIME_MAX_IDS = 25
_batch_routeid_pattern = re.compile(r"^[A-Za-z]{3}[A-Za-z0-9_-]+$")


@router.get("/api/v1/batchroutes/{routeids}/realtime")
def get_batch_routes_realtime(routeids: str, request: Request) -> dict:
    """Return realtime snapshots for up to 25 comma-separated route IDs.

    The server groups routes by city and makes one TDX batch request per
    city, which is far more efficient than the client calling the single-
    route endpoint 25 times and avoids 429 errors.
    """
    parts = [p.strip() for p in routeids.split(",") if p.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="No route IDs provided.")
    if len(parts) > _BATCH_ROUTES_REALTIME_MAX_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many route IDs (max {_BATCH_ROUTES_REALTIME_MAX_IDS}, got {len(parts)}).",
        )

    # Validate that each part looks like a plausible route ID.
    for part in parts:
        if not _batch_routeid_pattern.fullmatch(part):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid route ID: {part!r}",
            )

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)

    service = request.app.state.realtime_service
    try:
        batch = service.get_batch_snapshots(unique_parts)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc

    return {"routes": batch}


@router.get("/api/v1/routes/{routeid}/realtime")
def get_route_realtime(routeid: str, request: Request) -> dict:
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

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _search_routes(
            connection,
            routeid_like=f"{prefix}%",
            query=query,
            limit=limit,
        )


@router.get("/api/v1/routes")
def search_routes(
    request: Request,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=120, ge=1, le=300),
) -> list[dict]:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _search_routes(
            connection,
            routeid_like=None,
            query=query,
            limit=limit,
        )


@router.get("/api/v1/cities/{city}/stops/nearby")
def get_city_nearby_stops(
    city: str,
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(default=500, gt=0, le=3000),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[dict]:
    resolved_city = _resolve_city(city)
    if resolved_city is None:
        raise HTTPException(status_code=404, detail=f"City {city} was not found.")
    prefix, _ = resolved_city

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _load_nearby_stops(
            connection,
            routeid_like=f"{prefix}%",
            latitude=lat,
            longitude=lon,
            radius=radius,
            limit=limit,
        )


@router.get("/api/v1/routes/{routeid}/realtime/buses")
def get_route_buses(routeid: str, request: Request) -> list[dict]:
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


def _parse_tdx_datetime_to_unix(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_stop_ids(alert: dict) -> list[str]:
    stop_ids: list[str] = []
    scopes = alert.get("Scope") or alert.get("AlertScopes")
    if not scopes:
        return stop_ids
    if isinstance(scopes, dict):
        scopes = [scopes]
    if not isinstance(scopes, list):
        return stop_ids
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        stops = scope.get("Stops") or []
        for stop in stops:
            if not isinstance(stop, dict):
                continue
            stop_id = stop.get("StopID") or stop.get("StopUID") or ""
            if stop_id:
                stop_ids.append(str(stop_id))
    return stop_ids


def _alert_matches_route(alert: dict, routeid: str) -> bool:
    route_id = alert.get("RouteID") or ""
    route_uid = alert.get("RouteUID") or ""
    if routeid == route_id or routeid == route_uid:
        return True

    scopes = alert.get("Scope") or alert.get("AlertScopes")
    if not scopes:
        return False
    if isinstance(scopes, dict):
        scopes = [scopes]
    if not isinstance(scopes, list):
        return False
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for route in scope.get("Routes") or []:
            if isinstance(route, dict):
                if routeid in (route.get("RouteID", ""), route.get("RouteUID", "")):
                    return True
        for subroute in scope.get("SubRoutes") or []:
            if isinstance(subroute, dict):
                sub_uid = subroute.get("SubRouteUID") or subroute.get("SubRouteID") or ""
                if sub_uid.startswith(routeid):
                    return True
    return False


def _format_alert(alert: dict) -> dict:
    title_obj = alert.get("Title") or ""
    desc_obj = alert.get("Description") or ""
    scope_obj = alert.get("Scope") or ""
    if isinstance(title_obj, dict):
        title_obj = title_obj.get("Zh_tw") or title_obj.get("En") or ""
    if isinstance(desc_obj, dict):
        desc_obj = desc_obj.get("Zh_tw") or desc_obj.get("En") or ""
    if isinstance(scope_obj, dict):
        scope_obj = scope_obj.get("Zh_tw") or scope_obj.get("En") or ""
    direction = alert.get("Direction")
    if direction is None:
        scopes = alert.get("Scope") or alert.get("AlertScopes")
        if isinstance(scopes, dict):
            scopes = [scopes]
        if isinstance(scopes, list):
            for sc in scopes:
                if isinstance(sc, dict):
                    for sr in sc.get("SubRoutes") or []:
                        if isinstance(sr, dict) and sr.get("Direction") is not None:
                            direction = sr["Direction"]
                            break
                if direction is not None:
                    break
    return {
        "alert_id": alert.get("AlertID") or "",
        "title": str(title_obj),
        "description": str(desc_obj),
        "status": alert.get("Status"),
        "cause": alert.get("Cause"),
        "effect": alert.get("Effect"),
        "direction": direction,
        "scope": str(scope_obj) if scope_obj else None,
        "stop_ids": _extract_stop_ids(alert),
        "start_time": _parse_tdx_datetime_to_unix(alert.get("StartTime")),
        "end_time": _parse_tdx_datetime_to_unix(alert.get("EndTime")),
        "publish_time": _parse_tdx_datetime_to_unix(alert.get("PublishTime")),
        "updated_time": _parse_tdx_datetime_to_unix(alert.get("UpdateTime")),
    }


def _fetch_city_alerts_cached(request: Request, city_name: str) -> list[dict]:
    with _alerts_cache_lock:
        cached = _alerts_cache.get(city_name)
        if cached is not None and cached.is_fresh:
            return cached.alerts

    # Prevent thundering herd: only one thread fetches per city.
    with _alerts_in_flight_lock:
        # Double-check cache after acquiring lock.
        with _alerts_cache_lock:
            cached = _alerts_cache.get(city_name)
            if cached is not None and cached.is_fresh:
                return cached.alerts

    try:
        tdx_client = request.app.state.tdx_client
        raw_alerts = tdx_client.fetch_alerts(city_name)
        with _alerts_cache_lock:
            _alerts_cache[city_name] = _AlertsCacheEntry(alerts=raw_alerts)
        return raw_alerts
    except Exception as exc:
        LOGGER.warning("Failed to fetch alerts for %s: %s", city_name, exc)
        # Return stale cache if available.
        with _alerts_cache_lock:
            stale = _alerts_cache.get(city_name)
            if stale is not None:
                return stale.alerts
        raise


@router.get("/api/v1/routes/{routeuid}/alerts")
def get_route_alerts(routeuid: str, request: Request) -> dict:
    prefix = routeuid[:3].upper()
    city_name = CITY_PREFIX_TO_NAME.get(prefix)
    routeid = routeuid[3:]
    if city_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown city prefix for route {routeuid}.")

    try:
        city_alerts = _fetch_city_alerts_cached(request, city_name)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    matched = [
        _format_alert(alert)
        for alert in city_alerts
        if _alert_matches_route(alert, routeid)
    ]
    return {"routeid": routeuid, "alerts": matched}


@router.get("/api/v1/database/{name}/version")
def get_database_version(name: str, request: Request) -> dict:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        version = load_database_version(connection, name)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Database version for {name} was not found.")
    return version


@router.get("/api/v1/routes/{routeid}/operators")
def get_route_operators(routeid: str, request: Request) -> list[dict]:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT o.operator_id, o.name, o.name_en, o.code, o.phone, o.email, o.url
            FROM route_operators ro
            JOIN operators o ON o.operator_id = ro.operator_id
            WHERE ro.routeid = ?
            ORDER BY ro.seq
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


@router.get("/api/v1/routes/{routeid}/schedule")
def get_route_schedule(routeid: str, request: Request) -> list[dict]:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT subroute_uid, direction, kind, seq, service_days, payload
            FROM route_schedules
            WHERE routeid = ?
            ORDER BY subroute_uid, direction, kind, seq
            """,
            (routeid,),
        ).fetchall()
    import json as _json
    return [
        {
            "subroute_uid": row["subroute_uid"],
            "direction": row["direction"],
            "kind": row["kind"],
            "seq": row["seq"],
            "service_days": _json.loads(row["service_days"]),
            "payload": _json.loads(row["payload"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Taiwan holiday calendar
# ---------------------------------------------------------------------------

import json as _holiday_json
from functools import lru_cache
from pathlib import Path as _Path

_HOLIDAYS_FILE = _Path(__file__).resolve().parent.parent / "data" / "holidays.json"


@lru_cache(maxsize=1)
def _load_holidays_data() -> dict:
    """Loads and caches the bundled Taiwan holiday calendar JSON.

    The file is manually maintained (see app/data/holidays.json). Returns an
    empty structure if the file is missing or invalid so the endpoint never
    crashes.
    """
    try:
        with _HOLIDAYS_FILE.open("r", encoding="utf-8") as handle:
            data = _holiday_json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("holidays"), dict):
            return data
    except FileNotFoundError:
        LOGGER.warning("holidays.json not found at %s", _HOLIDAYS_FILE)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("failed to load holidays.json: %s", exc)
    return {"holidays": {}}


@router.get("/api/v1/holidays")
def get_holidays(
    request: Request,
    year: int | None = Query(default=None, description="Filter by year, e.g. 2026"),
    date: str | None = Query(
        default=None, description="Filter by a single date in YYYY-MM-DD"
    ),
) -> dict:
    """Returns the Taiwan national holiday calendar.

    - No params: returns all known holiday entries grouped by year.
    - ?year=2026: returns only that year's entries.
    - ?date=2026-02-18: returns a single object describing whether the date is
      a holiday (or make-up working day). Dates without an explicit entry fall
      back to the weekend rule (Sat/Sun = holiday).
    """
    data = _load_holidays_data()
    holidays_by_year: dict = data.get("holidays", {})

    if date is not None:
        try:
            parsed = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date format, expected YYYY-MM-DD")
        year_key = str(parsed.year)
        entry = None
        for item in holidays_by_year.get(year_key, []):
            if isinstance(item, dict) and item.get("date") == parsed.isoformat():
                entry = item
                break
        if entry is not None:
            is_holiday = bool(entry.get("isHoliday", True))
            name = entry.get("name")
        else:
            # No explicit entry: weekend dates are holidays, weekdays are not.
            is_weekend = parsed.weekday() >= 5  # 5=Sat, 6=Sun
            is_holiday = is_weekend
            name = None
        return {
            "date": parsed.isoformat(),
            "isHoliday": is_holiday,
            "name": name,
        }

    if year is not None:
        return {
            "year": year,
            "holidays": holidays_by_year.get(str(year), []),
        }

    return {
        "source": data.get("source"),
        "updated": data.get("updated"),
        "note": data.get("note"),
        "holidays": holidays_by_year,
    }
