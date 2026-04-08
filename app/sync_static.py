from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.db import get_connection, init_db
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient


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
        return f"往{destination}", None

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


def _replace_route(connection, route: StaticRoute) -> None:
    with connection:
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
                routes = client.fetch_routes(city)
                stop_of_route_items = client.fetch_stop_of_route(city)
                shapes = client.fetch_shapes(city)

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

                for route in static_routes.values():
                    _replace_route(connection, route)

                print(
                    f"[sync_static] city={city} routes={len(static_routes)} "
                    f"stop_of_route={len(stop_of_route_items)} shapes={len(shapes)}"
                )
    finally:
        client.close()
        token_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync static bus data from TDX.")
    parser.add_argument(
        "--cities",
        help="Comma-separated TDX city names. Defaults to TDX_CITIES or Taipei.",
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
