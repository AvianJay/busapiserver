from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import CITY_NAME_TO_PREFIX, Settings, get_settings
from app.db import (
    clear_inter_db,
    clear_city_db,
    delete_main_routes_by_prefix,
    export_download_db,
    get_connection,
    init_city_db,
    init_db,
    load_tdx_fetch_state,
    refresh_database_versions,
    save_tdx_fetch_state,
)
from app.logging_utils import get_logger, setup_logging, shutdown_logging
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient, TDXJSONResponse


POINT_PAIR_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")
LOGGER = get_logger("sync_static")


@dataclass
class StaticStop:
    seq: int
    stopid: str
    name: str
    name_en: str | None
    lat: float
    lon: float


@dataclass
class StaticPath:
    pathid: int
    name: str
    name_en: str | None = None
    stops: list[StaticStop] = field(default_factory=list)
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class StaticRoute:
    routeid: str
    name: str
    name_en: str | None
    route_uid: str | None = None
    departure: str | None = None
    departure_en: str | None = None
    destination: str | None = None
    destination_en: str | None = None
    operator_names: list[str] = field(default_factory=list)
    paths: dict[int, StaticPath] = field(default_factory=dict)


def _name_parts(value: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    zh = (value.get("Zh_tw") or "").strip() or None
    en = (value.get("En") or "").strip() or None
    return zh, en


def _name_text(value: dict | None) -> str | None:
    zh, en = _name_parts(value)
    return zh or en


def _optional_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _name_from_keys(
    item: dict | None,
    object_key: str,
    zh_key: str,
    en_key: str,
) -> tuple[str | None, str | None]:
    if not item:
        return None, None

    object_zh, object_en = _name_parts(item.get(object_key))
    zh = _optional_text(item.get(zh_key)) or object_zh
    en = _optional_text(item.get(en_key)) or object_en
    return zh, en


def _headsign_from_subroute(subroute: dict | None) -> tuple[str | None, str | None]:
    if not subroute:
        return None, None

    headsign = (subroute.get("HeadSign") or subroute.get("Headsign") or "").strip()
    headsign_en = (
        subroute.get("HeadSignEn")
        or subroute.get("HeadsignEn")
        or subroute.get("HeadSignEN")
        or ""
    ).strip()

    return headsign or None, headsign_en or None


def _terminal_name_from_route(route: dict | None, direction: int) -> tuple[str | None, str | None]:
    if not route:
        return None, None

    departure_zh, departure_en = _name_from_keys(
        route,
        "DepartureStopName",
        "DepartureStopNameZh",
        "DepartureStopNameEn",
    )
    destination_zh, destination_en = _name_from_keys(
        route,
        "DestinationStopName",
        "DestinationStopNameZh",
        "DestinationStopNameEn",
    )

    if direction == 0:
        return destination_zh or destination_en, destination_en
    if direction == 1:
        return departure_zh or departure_en, departure_en
    return None, None


def _path_name_from_route_and_subroute(
    route: dict | None,
    subroute: dict | None,
    direction: int,
) -> tuple[str, str | None]:
    terminal_name, terminal_name_en = _terminal_name_from_route(route, direction)
    if terminal_name or terminal_name_en:
        return terminal_name or terminal_name_en or "Unknown", terminal_name_en

    headsign, headsign_en = _headsign_from_subroute(subroute)
    if headsign or headsign_en:
        return headsign or headsign_en or "Unknown", headsign_en

    if not subroute:
        return "Unknown", None

    destination_zh, destination_en = _name_from_keys(
        subroute,
        "DestinationStopName",
        "DestinationStopNameZh",
        "DestinationStopNameEn",
    )
    destination = destination_zh or destination_en
    if destination:
        if destination_zh:
            return f"\u5f80{destination_zh}", (f"To {destination_en}" if destination_en else None)
        return destination, destination_en

    name_zh, name_en = _name_parts(subroute.get("SubRouteName"))
    return name_zh or name_en or subroute.get("SubRouteUID") or "Unknown", name_en


def _parse_geometry(geometry: str | None) -> list[tuple[float, float]]:
    if not geometry:
        return []

    points: list[tuple[float, float]] = []
    for lon_text, lat_text in POINT_PAIR_RE.findall(geometry):
        point = (float(lat_text), float(lon_text))
        if not points or points[-1] != point:
            points.append(point)
    return points


def _route_terminals_from_route(
    route: dict | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if not route:
        return None, None, None, None

    departure_zh, departure_en = _name_from_keys(
        route,
        "DepartureStopName",
        "DepartureStopNameZh",
        "DepartureStopNameEn",
    )
    destination_zh, destination_en = _name_from_keys(
        route,
        "DestinationStopName",
        "DestinationStopNameZh",
        "DestinationStopNameEn",
    )
    return departure_zh or departure_en, departure_en, destination_zh or destination_en, destination_en


def _operator_names_from_route(route: dict | None) -> list[str]:
    if not route:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for operator in route.get("Operators") or []:
        name = _name_text(operator.get("OperatorName"))
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _build_static_routes(
    routes: list[dict],
    stop_of_route_items: list[dict],
    shapes: list[dict],
) -> dict[str, StaticRoute]:
    subroute_lookup: dict[tuple[str, int], tuple[dict | None, dict | None]] = {}
    static_routes: dict[str, StaticRoute] = {}

    for route in routes:
        route_uid = route.get("RouteUID") or route.get("RouteID") or ""
        departure, departure_en, destination, destination_en = _route_terminals_from_route(route)
        operator_names = _operator_names_from_route(route)

        for item in route.get("SubRoutes") or []:
            routeid = item.get("SubRouteUID") or route_uid
            if not routeid:
                continue

            route_name, route_name_en = _name_parts(item.get("SubRouteName"))
            if not route_name:
                route_name, route_name_en = _name_parts(route.get("RouteName"))

            static_route = static_routes.setdefault(
                routeid,
                StaticRoute(
                    routeid=routeid,
                    name=route_name or routeid,
                    name_en=route_name_en,
                    route_uid=route_uid or routeid,
                    departure=departure,
                    departure_en=departure_en,
                    destination=destination,
                    destination_en=destination_en,
                    operator_names=list(operator_names),
                ),
            )

            if route_name and static_route.name == routeid:
                static_route.name = route_name
            if route_name_en and not static_route.name_en:
                static_route.name_en = route_name_en
            if route_uid and not static_route.route_uid:
                static_route.route_uid = route_uid
            if departure and not static_route.departure:
                static_route.departure = departure
            if departure_en and not static_route.departure_en:
                static_route.departure_en = departure_en
            if destination and not static_route.destination:
                static_route.destination = destination
            if destination_en and not static_route.destination_en:
                static_route.destination_en = destination_en
            for operator_name in operator_names:
                if operator_name not in static_route.operator_names:
                    static_route.operator_names.append(operator_name)

            pathid = int(item.get("Direction") or 0)
            path_name, path_name_en = _path_name_from_route_and_subroute(route, item, pathid)
            static_route.paths.setdefault(
                pathid,
                StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
            )
            subroute_lookup[(routeid, pathid)] = (route, item)

    for item in stop_of_route_items:
        routeid = item.get("SubRouteUID") or item.get("RouteUID")
        if not routeid:
            continue

        pathid = int(item.get("Direction") or 0)
        static_route = static_routes.setdefault(
            routeid,
            StaticRoute(routeid=routeid, name=routeid, name_en=None, route_uid=item.get("RouteUID") or routeid),
        )

        route_item, subroute = subroute_lookup.get((routeid, pathid), (None, None))
        path_name, path_name_en = _path_name_from_route_and_subroute(route_item, subroute, pathid)
        path = static_route.paths.setdefault(
            pathid,
            StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
        )

        if not path.stops:
            stops: list[StaticStop] = []
            for stop in item.get("Stops") or []:
                stop_name, stop_name_en = _name_parts(stop.get("StopName"))
                position = stop.get("StopPosition") or {}
                lat = position.get("PositionLat")
                lon = position.get("PositionLon")
                if lat is None or lon is None:
                    continue
                stops.append(
                    StaticStop(
                        seq=int(stop.get("StopSequence") or 0),
                        stopid=stop.get("StopID") or stop.get("StopUID") or "",
                        name=stop_name or stop.get("StopID") or "Unknown",
                        name_en=stop_name_en,
                        lat=float(lat),
                        lon=float(lon),
                    )
                )
            path.stops = stops

    for item in shapes:
        routeid = item.get("SubRouteUID") or item.get("RouteUID")
        if not routeid:
            continue

        pathid = int(item.get("Direction") or 0)
        static_route = static_routes.setdefault(
            routeid,
            StaticRoute(routeid=routeid, name=routeid, name_en=None, route_uid=item.get("RouteUID") or routeid),
        )
        route_item, subroute = subroute_lookup.get((routeid, pathid), (None, None))
        path_name, path_name_en = _path_name_from_route_and_subroute(route_item, subroute, pathid)
        path = static_route.paths.setdefault(
            pathid,
            StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
        )
        if not path.points:
            path.points = _parse_geometry(item.get("Geometry"))

    _normalize_path_names_from_stops(static_routes)
    return static_routes


def _replace_main_route(connection, route: StaticRoute) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO routes (routeid, name, name_en)
        VALUES (?, ?, ?)
        """,
        (route.routeid, route.name, route.name_en),
    )
    connection.execute("DELETE FROM paths WHERE routeid = ?", (route.routeid,))

    for path in sorted(route.paths.values(), key=lambda item: item.pathid):
        connection.execute(
            """
            INSERT OR REPLACE INTO paths (routeid, pathid, name, name_en)
            VALUES (?, ?, ?, ?)
            """,
            (route.routeid, path.pathid, path.name, path.name_en),
        )

        for stop in sorted(path.stops, key=lambda item: item.seq):
            connection.execute(
                """
                INSERT OR REPLACE INTO stops
                (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route.routeid,
                    path.pathid,
                    stop.seq,
                    stop.stopid,
                    stop.name,
                    stop.name_en,
                    stop.lat,
                    stop.lon,
                ),
            )

        for seq, (lat, lon) in enumerate(path.points, start=1):
            connection.execute(
                """
                INSERT OR REPLACE INTO path_points
                (routeid, pathid, seq, lat, lon)
                VALUES (?, ?, ?, ?, ?)
                """,
                (route.routeid, path.pathid, seq, lat, lon),
            )


def _replace_city_route(connection, route: StaticRoute) -> None:
    connection.execute("DELETE FROM stops WHERE routeid = ?", (route.routeid,))

    for path in sorted(route.paths.values(), key=lambda item: item.pathid):
        for stop in sorted(path.stops, key=lambda item: item.seq):
            connection.execute(
                """
                INSERT OR REPLACE INTO stops
                (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route.routeid,
                    path.pathid,
                    stop.seq,
                    stop.stopid,
                    stop.name,
                    stop.name_en,
                    stop.lat,
                    stop.lon,
                ),
            )


def _replace_inter_route(connection, route: StaticRoute) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO inter_routes
            (routeid, route_uid, name, name_en, departure, departure_en, destination, destination_en, operator_names)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            route.routeid,
            route.route_uid or route.routeid,
            route.name,
            route.name_en,
            route.departure,
            route.departure_en,
            route.destination,
            route.destination_en,
            " / ".join(route.operator_names),
        ),
    )
    connection.execute("DELETE FROM inter_paths WHERE routeid = ?", (route.routeid,))

    for path in sorted(route.paths.values(), key=lambda item: item.pathid):
        connection.execute(
            """
            INSERT OR REPLACE INTO inter_paths (routeid, pathid, name, name_en)
            VALUES (?, ?, ?, ?)
            """,
            (route.routeid, path.pathid, path.name, path.name_en),
        )

        for stop in sorted(path.stops, key=lambda item: item.seq):
            connection.execute(
                """
                INSERT OR REPLACE INTO inter_stops
                (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route.routeid,
                    path.pathid,
                    stop.seq,
                    stop.stopid,
                    stop.name,
                    stop.name_en,
                    stop.lat,
                    stop.lon,
                ),
            )

        for seq, (lat, lon) in enumerate(path.points, start=1):
            connection.execute(
                """
                INSERT OR REPLACE INTO inter_path_points
                (routeid, pathid, seq, lat, lon)
                VALUES (?, ?, ?, ?, ?)
                """,
                (route.routeid, path.pathid, seq, lat, lon),
            )


def _city_temp_db_path(city_db_path: Path) -> Path:
    return city_db_path.with_suffix(f"{city_db_path.suffix}.tmp")


def _should_use_terminal_stop_name(path_name: str | None, route_name: str | None) -> bool:
    if not path_name:
        return True

    normalized_path_name = path_name.strip().lower()
    if normalized_path_name in {"unknown", "path 0", "path 1"}:
        return True

    normalized_route_name = (route_name or "").strip().lower()
    if normalized_route_name and normalized_path_name == normalized_route_name:
        return True

    # Keep subroute headsign text intact (e.g. "A - B - A").
    return False


def _normalize_path_names_from_stops(static_routes: dict[str, StaticRoute]) -> None:
    for route in static_routes.values():
        for path in route.paths.values():
            if not path.stops:
                continue
            if not _should_use_terminal_stop_name(path.name, route.name):
                continue

            terminal_stop = path.stops[-1]
            if terminal_stop.name:
                path.name = terminal_stop.name
            if terminal_stop.name_en:
                path.name_en = terminal_stop.name_en


def _resource_key(city: str, resource: str) -> str:
    return f"static:{city}:{resource}"


def _upsert_operators(connection, operators_raw: list[dict]) -> None:
    for op in operators_raw:
        op_id = op.get("OperatorID")
        if not op_id:
            continue
        op_name_zh, op_name_en = _name_parts(op.get("OperatorName"))
        connection.execute(
            """
            INSERT OR REPLACE INTO operators
                (operator_id, name, name_en, code, phone, email, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                op_id,
                op_name_zh or op_id,
                op_name_en,
                _optional_text(op.get("OperatorCode")),
                _optional_text(op.get("OperatorPhone")),
                _optional_text(op.get("OperatorEmail")),
                _optional_text(op.get("OperatorUrl")),
            ),
        )


def _sync_route_operator_links(
    connection,
    routes: list[dict],
    static_route_ids: set[str],
    *,
    table_name: str,
) -> None:
    for route_item in routes:
        route_uid = route_item.get("RouteUID") or route_item.get("RouteID")
        if not route_uid:
            continue
        for seq, op_ref in enumerate(route_item.get("Operators") or []):
            op_id = op_ref.get("OperatorID")
            if not op_id:
                continue
            for sub in route_item.get("SubRoutes") or []:
                sub_uid = sub.get("SubRouteUID") or route_uid
                if sub_uid not in static_route_ids:
                    continue
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {table_name}
                        (routeid, operator_id, seq)
                    VALUES (?, ?, ?)
                    """,
                    (sub_uid, op_id, seq),
                )


def _sync_route_schedules(
    connection,
    schedules_raw: list[dict],
    static_route_ids: set[str],
    *,
    table_name: str,
) -> None:
    for sched in schedules_raw:
        route_uid = sched.get("SubRouteUID") or sched.get("RouteUID")
        if not route_uid or route_uid not in static_route_ids:
            continue
        direction = int(sched.get("Direction") or 0)
        subroute_uid = sched.get("SubRouteUID") or ""

        for freq_seq, freq in enumerate(sched.get("Frequencys") or []):
            sd = freq.get("ServiceDay") or {}
            service_days = json.dumps({
                "mon": sd.get("Monday", 0),
                "tue": sd.get("Tuesday", 0),
                "wed": sd.get("Wednesday", 0),
                "thu": sd.get("Thursday", 0),
                "fri": sd.get("Friday", 0),
                "sat": sd.get("Saturday", 0),
                "sun": sd.get("Sunday", 0),
                "holiday": sd.get("NationalHolidays", 0),
            }, ensure_ascii=False)
            payload = json.dumps({
                "start": freq.get("StartTime"),
                "end": freq.get("EndTime"),
                "min_headway": freq.get("MinHeadwayMins"),
                "max_headway": freq.get("MaxHeadwayMins"),
            }, ensure_ascii=False)
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {table_name}
                    (routeid, subroute_uid, direction, kind, seq, service_days, payload)
                VALUES (?, ?, ?, 'frequency', ?, ?, ?)
                """,
                (route_uid, subroute_uid, direction, freq_seq, service_days, payload),
            )

        for tt_seq, tt in enumerate(sched.get("Timetables") or []):
            sd = tt.get("ServiceDay") or {}
            service_days = json.dumps({
                "mon": sd.get("Monday", 0),
                "tue": sd.get("Tuesday", 0),
                "wed": sd.get("Wednesday", 0),
                "thu": sd.get("Thursday", 0),
                "fri": sd.get("Friday", 0),
                "sat": sd.get("Saturday", 0),
                "sun": sd.get("Sunday", 0),
                "holiday": sd.get("NationalHolidays", 0),
            }, ensure_ascii=False)
            stop_times = []
            for st in tt.get("StopTimes") or []:
                stop_times.append({
                    "seq": st.get("StopSequence"),
                    "stopid": st.get("StopID") or st.get("StopUID") or "",
                    "arrival": st.get("ArrivalTime"),
                    "departure": st.get("DepartureTime"),
                })
            payload = json.dumps({
                "trip_id": tt.get("TripID"),
                "is_low_floor": tt.get("IsLowFloor"),
                "stop_times": stop_times,
            }, ensure_ascii=False)
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {table_name}
                    (routeid, subroute_uid, direction, kind, seq, service_days, payload)
                VALUES (?, ?, ?, 'timetable', ?, ?, ?)
                """,
                (route_uid, subroute_uid, direction, tt_seq, service_days, payload),
            )


def _persist_fetch_state(
    connection,
    city: str,
    resource: str,
    response: TDXJSONResponse,
    checked_at: int,
    previous_state: dict | None,
) -> None:
    previous_last_modified = None if previous_state is None else previous_state.get("last_modified")
    previous_updated_at = None if previous_state is None else previous_state.get("last_updated_at")
    effective_last_modified = response.last_modified or previous_last_modified

    save_tdx_fetch_state(
        connection,
        _resource_key(city, resource),
        last_modified=effective_last_modified,
        last_status=response.status_code,
        last_checked_at=checked_at,
        last_updated_at=checked_at if not response.not_modified else previous_updated_at,
    )


def sync_static(
    settings: Settings,
    cities: tuple[str, ...] | None = None,
    *,
    force: bool = False,
) -> None:
    settings.require_tdx_credentials()
    init_db(settings.db_path)

    effective_cities = cities or settings.tdx_cities
    token_manager = TDXTokenManager(settings)
    client = TDXClient(settings, token_manager)

    try:
        with get_connection(settings.db_path) as connection:
            for city in effective_cities:
                LOGGER.info("syncing city=%s", city)

                routes_state = load_tdx_fetch_state(connection, _resource_key(city, "routes"))
                stops_state = load_tdx_fetch_state(connection, _resource_key(city, "stop_of_route"))
                shapes_state = load_tdx_fetch_state(connection, _resource_key(city, "shapes"))

                routes_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Route/City/{city}",
                    if_modified_since=None
                    if force or routes_state is None
                    else routes_state.get("last_modified"),
                )
                stops_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/StopOfRoute/City/{city}",
                    if_modified_since=None
                    if force or stops_state is None
                    else stops_state.get("last_modified"),
                )
                shapes_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Shape/City/{city}",
                    if_modified_since=None
                    if force or shapes_state is None
                    else shapes_state.get("last_modified"),
                )

                checked_at = int(time.time())
                _persist_fetch_state(connection, city, "routes", routes_response, checked_at, routes_state)
                _persist_fetch_state(connection, city, "stop_of_route", stops_response, checked_at, stops_state)
                _persist_fetch_state(connection, city, "shapes", shapes_response, checked_at, shapes_state)

                if (
                    routes_response.not_modified
                    and stops_response.not_modified
                    and shapes_response.not_modified
                ):
                    LOGGER.info("city=%s no static updates (all resources returned 304)", city)
                    continue

                if routes_response.not_modified:
                    routes_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/Route/City/{city}",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, city, "routes", routes_response, checked_at, routes_state)

                if stops_response.not_modified:
                    stops_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/StopOfRoute/City/{city}",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, city, "stop_of_route", stops_response, checked_at, stops_state)

                if shapes_response.not_modified:
                    shapes_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/Shape/City/{city}",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, city, "shapes", shapes_response, checked_at, shapes_state)

                routes = routes_response.payload or []
                stop_of_route_items = stops_response.payload or []
                shapes = shapes_response.payload or []
                static_routes = _build_static_routes(routes, stop_of_route_items, shapes)

                prefix = CITY_NAME_TO_PREFIX.get(city)
                if prefix:
                    with connection:
                        delete_main_routes_by_prefix(connection, prefix)
                        for route in static_routes.values():
                            _replace_main_route(connection, route)

                # --- Sync Operators ---
                operators_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Operator/City/{city}",
                    if_modified_since=None,
                )
                operators_raw = operators_response.payload or []
                if prefix:
                    with connection:
                        connection.execute(
                            "DELETE FROM route_operators WHERE routeid LIKE ?",
                            (f"{prefix}%",),
                        )
                        _upsert_operators(connection, operators_raw)
                        _sync_route_operator_links(
                            connection,
                            routes,
                            set(static_routes),
                            table_name="route_operators",
                        )

                # --- Sync Schedules ---
                schedules_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Schedule/City/{city}",
                    if_modified_since=None,
                )
                schedules_raw = schedules_response.payload or []
                if prefix:
                    with connection:
                        connection.execute(
                            "DELETE FROM route_schedules WHERE routeid LIKE ?",
                            (f"{prefix}%",),
                        )
                        _sync_route_schedules(
                            connection,
                            schedules_raw,
                            set(static_routes),
                            table_name="route_schedules",
                        )

                city_db_path = settings.city_db_path(city)
                temp_city_db_path = _city_temp_db_path(city_db_path)
                if temp_city_db_path.exists():
                    temp_city_db_path.unlink()
                init_city_db(temp_city_db_path)
                with get_connection(temp_city_db_path) as city_connection:
                    with city_connection:
                        clear_city_db(city_connection)
                        for route in static_routes.values():
                            _replace_city_route(city_connection, route)
                city_db_path.parent.mkdir(parents=True, exist_ok=True)
                temp_city_db_path.replace(city_db_path)

                LOGGER.info(
                    "city=%s routes=%s stop_of_route=%s shapes=%s city_db=%s "
                    "status(routes/stops/shapes)=%s/%s/%s",
                    city,
                    len(static_routes),
                    len(stop_of_route_items),
                    len(shapes),
                    city_db_path.name,
                    routes_response.status_code,
                    stops_response.status_code,
                    shapes_response.status_code,
                )

            inter_name = "InterCity"
            LOGGER.info("syncing intercity")

            inter_routes_state = load_tdx_fetch_state(connection, _resource_key(inter_name, "routes"))
            inter_stops_state = load_tdx_fetch_state(connection, _resource_key(inter_name, "stop_of_route"))
            inter_shapes_state = load_tdx_fetch_state(connection, _resource_key(inter_name, "shapes"))

            inter_routes_response = client.fetch_paginated_items_conditional(
                "/v2/Bus/Route/InterCity",
                if_modified_since=None
                if force or inter_routes_state is None
                else inter_routes_state.get("last_modified"),
            )
            inter_stops_response = client.fetch_paginated_items_conditional(
                "/v2/Bus/StopOfRoute/InterCity",
                if_modified_since=None
                if force or inter_stops_state is None
                else inter_stops_state.get("last_modified"),
            )
            inter_shapes_response = client.fetch_paginated_items_conditional(
                "/v2/Bus/Shape/InterCity",
                if_modified_since=None
                if force or inter_shapes_state is None
                else inter_shapes_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, inter_name, "routes", inter_routes_response, checked_at, inter_routes_state)
            _persist_fetch_state(connection, inter_name, "stop_of_route", inter_stops_response, checked_at, inter_stops_state)
            _persist_fetch_state(connection, inter_name, "shapes", inter_shapes_response, checked_at, inter_shapes_state)

            if (
                inter_routes_response.not_modified
                and inter_stops_response.not_modified
                and inter_shapes_response.not_modified
            ):
                LOGGER.info("intercity no static updates (all resources returned 304)")
            else:
                if inter_routes_response.not_modified:
                    inter_routes_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/Route/InterCity",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, inter_name, "routes", inter_routes_response, checked_at, inter_routes_state)

                if inter_stops_response.not_modified:
                    inter_stops_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/StopOfRoute/InterCity",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, inter_name, "stop_of_route", inter_stops_response, checked_at, inter_stops_state)

                if inter_shapes_response.not_modified:
                    inter_shapes_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/Shape/InterCity",
                        if_modified_since=None,
                    )
                    _persist_fetch_state(connection, inter_name, "shapes", inter_shapes_response, checked_at, inter_shapes_state)

                inter_routes_raw = inter_routes_response.payload or []
                inter_stop_of_route_items = inter_stops_response.payload or []
                inter_shapes_raw = inter_shapes_response.payload or []
                inter_static_routes = _build_static_routes(
                    inter_routes_raw,
                    inter_stop_of_route_items,
                    inter_shapes_raw,
                )
                inter_operators_raw = client.fetch_paginated_items("/v2/Bus/Operator/InterCity")
                inter_schedules_raw = client.fetch_paginated_items("/v2/Bus/Schedule/InterCity")

                with connection:
                    clear_inter_db(connection)
                    for route in inter_static_routes.values():
                        _replace_inter_route(connection, route)
                    connection.execute("DELETE FROM inter_route_operators")
                    connection.execute("DELETE FROM inter_route_schedules")
                    _upsert_operators(connection, inter_operators_raw)
                    _sync_route_operator_links(
                        connection,
                        inter_routes_raw,
                        set(inter_static_routes),
                        table_name="inter_route_operators",
                    )
                    _sync_route_schedules(
                        connection,
                        inter_schedules_raw,
                        set(inter_static_routes),
                        table_name="inter_route_schedules",
                    )

                LOGGER.info(
                    "intercity routes=%s stop_of_route=%s shapes=%s status(routes/stops/shapes)=%s/%s/%s",
                    len(inter_static_routes),
                    len(inter_stop_of_route_items),
                    len(inter_shapes_raw),
                    inter_routes_response.status_code,
                    inter_stops_response.status_code,
                    inter_shapes_response.status_code,
                )

        export_download_db(settings.db_path, settings.download_db_path)
        refresh_database_versions(
            settings.db_path,
            download_db_path=settings.download_db_path,
            city_db_paths={city: settings.city_db_path(city) for city in effective_cities},
            force=force,
        )
        LOGGER.info("built download db at %s", settings.download_db_path)
    finally:
        client.close()
        token_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync static bus data from TDX.")
    parser.add_argument(
        "--cities",
        help="Comma-separated TDX city names. Defaults to TDX_CITIES or all supported CityBus cities/counties.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force full re-fetch without If-Modified-Since and force database version increment.",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.project_dir)
    cities = None
    if args.cities:
        cities = tuple(
            item.strip() for item in args.cities.split(",") if item.strip()
        ) or settings.tdx_cities

    try:
        sync_static(settings, cities=cities, force=args.force)
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
