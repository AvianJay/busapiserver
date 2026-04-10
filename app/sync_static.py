from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import CITY_NAME_TO_PREFIX, Settings, get_settings
from app.db import (
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
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient, TDXJSONResponse


POINT_PAIR_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")


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


def _path_name_from_subroute(subroute: dict | None) -> tuple[str, str | None]:
    if not subroute:
        return "Unknown", None

    headsign = (subroute.get("HeadSign") or subroute.get("Headsign") or "").strip()
    if headsign:
        return headsign, None

    destination = (
        _name_text(subroute.get("DestinationStopName"))
        or (subroute.get("DestinationStopNameZh") or "").strip()
        or (subroute.get("DestinationStopNameEn") or "").strip()
    )
    if destination:
        return f"\u5f80{destination}", None

    name_zh, name_en = _name_parts(subroute.get("SubRouteName"))
    return name_zh or subroute.get("SubRouteUID") or "Unknown", name_en


def _parse_geometry(geometry: str | None) -> list[tuple[float, float]]:
    if not geometry:
        return []

    points: list[tuple[float, float]] = []
    for lon_text, lat_text in POINT_PAIR_RE.findall(geometry):
        point = (float(lat_text), float(lon_text))
        if not points or points[-1] != point:
            points.append(point)
    return points


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
    city_code = route.routeid[:3].upper()
    connection.execute(
        """
        INSERT OR REPLACE INTO routes (routeid, name, name_en, city_code)
        VALUES (?, ?, ?, ?)
        """,
        (route.routeid, route.name, route.name_en, city_code),
    )
    connection.execute("DELETE FROM paths WHERE routeid = ?", (route.routeid,))
    connection.execute("DELETE FROM stops WHERE routeid = ?", (route.routeid,))

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


def _city_temp_db_path(city_db_path: Path) -> Path:
    return city_db_path.with_suffix(f"{city_db_path.suffix}.tmp")


def _should_use_terminal_stop_name(path_name: str | None) -> bool:
    if not path_name:
        return True
    return any(separator in path_name for separator in (" - ", "－", "|", "→", "<->", "↔"))


def _normalize_path_names_from_stops(static_routes: dict[str, StaticRoute]) -> None:
    for route in static_routes.values():
        for path in route.paths.values():
            if not path.stops:
                continue
            if not _should_use_terminal_stop_name(path.name):
                continue

            terminal_stop = path.stops[-1]
            if terminal_stop.name:
                path.name = terminal_stop.name
            if terminal_stop.name_en:
                path.name_en = terminal_stop.name_en


def _resource_key(city: str, resource: str) -> str:
    return f"static:{city}:{resource}"


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


def sync_static(settings: Settings, cities: tuple[str, ...] | None = None) -> None:
    settings.require_tdx_credentials()
    init_db(settings.db_path)

    effective_cities = cities or settings.tdx_cities
    token_manager = TDXTokenManager(settings)
    client = TDXClient(settings, token_manager)

    try:
        with get_connection(settings.db_path) as connection:
            for city in effective_cities:
                print(f"[sync_static] syncing city={city}")

                routes_state = load_tdx_fetch_state(connection, _resource_key(city, "routes"))
                stops_state = load_tdx_fetch_state(connection, _resource_key(city, "stop_of_route"))
                shapes_state = load_tdx_fetch_state(connection, _resource_key(city, "shapes"))

                routes_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Route/City/{city}",
                    if_modified_since=None if routes_state is None else routes_state.get("last_modified"),
                )
                stops_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/StopOfRoute/City/{city}",
                    if_modified_since=None if stops_state is None else stops_state.get("last_modified"),
                )
                shapes_response = client.fetch_paginated_items_conditional(
                    f"/v2/Bus/Shape/City/{city}",
                    if_modified_since=None if shapes_state is None else shapes_state.get("last_modified"),
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
                    print(f"[sync_static] city={city} no static updates (all resources returned 304)")
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

                subroute_lookup = {}
                static_routes: dict[str, StaticRoute] = {}

                for route in routes:
                    for item in route.get("SubRoutes") or []:
                        routeid = item.get("SubRouteUID") or route.get("RouteUID")
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
                            ),
                        )

                        pathid = int(item.get("Direction") or 0)
                        path_name, path_name_en = _path_name_from_subroute(item)
                        static_route.paths.setdefault(
                            pathid,
                            StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
                        )
                        subroute_lookup[(routeid, pathid)] = item

                for item in stop_of_route_items:
                    routeid = item.get("SubRouteUID") or item.get("RouteUID")
                    if not routeid:
                        continue

                    pathid = int(item.get("Direction") or 0)
                    static_route = static_routes.setdefault(
                        routeid,
                        StaticRoute(routeid=routeid, name=routeid, name_en=None),
                    )

                    subroute = subroute_lookup.get((routeid, pathid))
                    path_name, path_name_en = _path_name_from_subroute(subroute)
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
                        StaticRoute(routeid=routeid, name=routeid, name_en=None),
                    )
                    subroute = subroute_lookup.get((routeid, pathid))
                    path_name, path_name_en = _path_name_from_subroute(subroute)
                    path = static_route.paths.setdefault(
                        pathid,
                        StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
                    )
                    if not path.points:
                        path.points = _parse_geometry(item.get("Geometry"))

                _normalize_path_names_from_stops(static_routes)

                prefix = CITY_NAME_TO_PREFIX.get(city)
                if prefix:
                    with connection:
                        delete_main_routes_by_prefix(connection, prefix)
                        for route in static_routes.values():
                            _replace_main_route(connection, route)

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

                print(
                    f"[sync_static] city={city} routes={len(static_routes)} "
                    f"stop_of_route={len(stop_of_route_items)} shapes={len(shapes)} "
                    f"city_db={city_db_path.name} "
                    f"status(routes/stops/shapes)="
                    f"{routes_response.status_code}/{stops_response.status_code}/{shapes_response.status_code}"
                )

        export_download_db(settings.db_path, settings.download_db_path)
        refresh_database_versions(
            settings.db_path,
            download_db_path=settings.download_db_path,
            city_db_paths={city: settings.city_db_path(city) for city in effective_cities},
        )
        print(f"[sync_static] built download db at {settings.download_db_path}")
    finally:
        client.close()
        token_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync static bus data from TDX.")
    parser.add_argument(
        "--cities",
        help="Comma-separated TDX city names. Defaults to TDX_CITIES or all supported CityBus cities/counties.",
    )
    args = parser.parse_args()

    settings = get_settings()
    cities = None
    if args.cities:
        cities = tuple(
            item.strip() for item in args.cities.split(",") if item.strip()
        ) or settings.tdx_cities

    sync_static(settings, cities=cities)


if __name__ == "__main__":
    main()
