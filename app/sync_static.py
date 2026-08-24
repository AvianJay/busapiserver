from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    CITY_NAME_TO_PREFIX,
    INTERCITY_CITY_NAME,
    INTERCITY_PREFIX,
    Settings,
    from_intercity_routeid,
    get_settings,
    to_intercity_routeid,
)
from app.db import (
    clear_city_db,
    delete_main_routes_by_prefix,
    export_download_db,
    get_connection,
    has_route_subroutes_table,
    has_route_uids_table,
    init_city_db,
    init_db,
    load_route_subroute_map,
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
    # Every SubRouteUID this route was built from, mapped to its Direction.
    # Usually just the routeid itself; a merged route also lists the sibling it
    # absorbed. Persisted to route_subroutes.
    subroute_uids: dict[str, int] = field(default_factory=dict)


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


def _subroute_group_key(route: dict, subroute: dict) -> tuple[str, str] | None:
    """Key that identifies direction siblings of the same line.

    ``RouteUID`` alone is too coarse: under 公路客運 the lettered variants
    (1815, 1815A … 1815G) all share one RouteUID, so the SubRouteName has to be
    part of the key. Only ``.strip()`` is applied — any further normalization
    (casefolding, width folding) would risk merging genuinely distinct routes.
    """
    route_uid = (route.get("RouteUID") or route.get("RouteID") or "").strip()
    subroute_name, _ = _name_parts(subroute.get("SubRouteName"))
    if not route_uid or not subroute_name:
        return None
    return route_uid, subroute_name


def _choose_canonical(
    members: list[tuple[str, int]],
    previous_canonical: dict[str, str],
) -> str:
    """Pick the SubRouteUID that survives a merge.

    Lowest direction wins, ties broken lexicographically — but if a previous sync
    already picked a canonical that is still part of this group, keep it. Without
    that override, TDX later publishing a direction 0 for a group that is
    currently direction-1-only would flip the surviving routeid and break a
    second wave of saved favorites.
    """
    member_uids = {subroute_uid for subroute_uid, _ in members}
    for subroute_uid, _direction in members:
        candidate = previous_canonical.get(subroute_uid)
        if candidate and candidate in member_uids:
            return candidate
    return members[0][0]


def _group_subroutes(
    routes: list[dict],
    previous_canonical: dict[str, str],
) -> dict[str, str]:
    """Map absorbed SubRouteUIDs to the canonical routeid that replaces them.

    Only genuine direction siblings are merged; singletons are absent from the
    result so callers fall back to identity.
    """
    grouped: dict[tuple[str, str], dict[str, set[int]]] = {}
    for route in routes:
        for subroute in route.get("SubRoutes") or []:
            subroute_uid = (subroute.get("SubRouteUID") or "").strip()
            if not subroute_uid:
                continue
            key = _subroute_group_key(route, subroute)
            if key is None:
                continue
            directions = grouped.setdefault(key, {}).setdefault(subroute_uid, set())
            directions.add(int(subroute.get("Direction") or 0))

    alias_map: dict[str, str] = {}
    for (route_uid, subroute_name), directions_by_uid in grouped.items():
        if len(directions_by_uid) < 2:
            continue

        # A SubRouteUID that already carries both directions is the shape the
        # six metros use, and it is already correct. Merging anything on top of
        # it would be guesswork, so leave the whole group alone.
        if any(len(directions) > 1 for directions in directions_by_uid.values()):
            LOGGER.warning(
                "skipping direction merge route_uid=%s subroute=%s: a subroute already spans directions",
                route_uid,
                subroute_name,
            )
            continue

        members = sorted(
            ((uid, next(iter(directions))) for uid, directions in directions_by_uid.items()),
            key=lambda member: (member[1], member[0]),
        )
        directions = [direction for _, direction in members]
        if len(set(directions)) != len(directions):
            LOGGER.warning(
                "skipping direction merge route_uid=%s subroute=%s: duplicate directions %s",
                route_uid,
                subroute_name,
                sorted(directions),
            )
            continue

        canonical = _choose_canonical(members, previous_canonical)
        for subroute_uid, _direction in members:
            if subroute_uid != canonical:
                alias_map[subroute_uid] = canonical

    return alias_map


def _alias_map_from_routes(static_routes: dict[str, StaticRoute]) -> dict[str, str]:
    """Derive SubRouteUID -> routeid from built routes, in their own namespace."""
    return {
        subroute_uid: route.routeid
        for route in static_routes.values()
        for subroute_uid in route.subroute_uids
    }


def _build_static_routes(
    routes: list[dict],
    stop_of_route_items: list[dict],
    shapes: list[dict],
    *,
    merge_directions: bool = True,
    previous_canonical: dict[str, str] | None = None,
) -> dict[str, StaticRoute]:
    subroute_lookup: dict[tuple[str, int], tuple[dict | None, dict | None]] = {}
    static_routes: dict[str, StaticRoute] = {}
    alias_map = (
        _group_subroutes(routes, previous_canonical or {}) if merge_directions else {}
    )

    for route in routes:
        route_uid = route.get("RouteUID") or route.get("RouteID") or ""
        departure, departure_en, destination, destination_en = _route_terminals_from_route(route)
        operator_names = _operator_names_from_route(route)

        for item in route.get("SubRoutes") or []:
            subroute_uid = item.get("SubRouteUID") or route_uid
            if not subroute_uid:
                continue
            routeid = alias_map.get(subroute_uid, subroute_uid)

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
            static_route.subroute_uids.setdefault(subroute_uid, pathid)
            path_name, path_name_en = _path_name_from_route_and_subroute(route, item, pathid)
            static_route.paths.setdefault(
                pathid,
                StaticPath(pathid=pathid, name=path_name, name_en=path_name_en),
            )
            subroute_lookup[(routeid, pathid)] = (route, item)

    for item in stop_of_route_items:
        subroute_uid = item.get("SubRouteUID") or item.get("RouteUID")
        if not subroute_uid:
            continue
        routeid = alias_map.get(subroute_uid, subroute_uid)

        pathid = int(item.get("Direction") or 0)
        static_route = static_routes.setdefault(
            routeid,
            StaticRoute(routeid=routeid, name=routeid, name_en=None, route_uid=item.get("RouteUID") or routeid),
        )
        static_route.subroute_uids.setdefault(subroute_uid, pathid)

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
        subroute_uid = item.get("SubRouteUID") or item.get("RouteUID")
        if not subroute_uid:
            continue
        routeid = alias_map.get(subroute_uid, subroute_uid)

        pathid = int(item.get("Direction") or 0)
        static_route = static_routes.setdefault(
            routeid,
            StaticRoute(routeid=routeid, name=routeid, name_en=None, route_uid=item.get("RouteUID") or routeid),
        )
        static_route.subroute_uids.setdefault(subroute_uid, pathid)
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


def _copy_route_with_routeid(
    route: StaticRoute,
    routeid: str,
    *,
    subroute_uid_mapper: Callable[[str], str] | None = None,
) -> StaticRoute:
    mapper = subroute_uid_mapper or (lambda value: value)
    return StaticRoute(
        routeid=routeid,
        name=route.name,
        name_en=route.name_en,
        route_uid=route.route_uid,
        departure=route.departure,
        departure_en=route.departure_en,
        destination=route.destination,
        destination_en=route.destination_en,
        operator_names=list(route.operator_names),
        paths=dict(route.paths),
        subroute_uids={
            mapper(subroute_uid): direction
            for subroute_uid, direction in route.subroute_uids.items()
        },
    )


def _prefix_inter_routes(static_routes: dict[str, StaticRoute]) -> dict[str, StaticRoute]:
    return {
        to_intercity_routeid(routeid): _copy_route_with_routeid(
            route,
            to_intercity_routeid(routeid),
            subroute_uid_mapper=to_intercity_routeid,
        )
        for routeid, route in static_routes.items()
    }


def _replace_main_route(connection, route: StaticRoute) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO routes (routeid, name, name_en)
        VALUES (?, ?, ?)
        """,
        (route.routeid, route.name, route.name_en),
    )
    connection.execute("DELETE FROM paths WHERE routeid = ?", (route.routeid,))
    connection.execute("DELETE FROM route_subroutes WHERE routeid = ?", (route.routeid,))
    connection.execute("DELETE FROM route_uids WHERE routeid = ?", (route.routeid,))

    # Sorted so that repeated syncs produce byte-identical rows; database
    # versions are content-hash driven.
    subroute_uids = route.subroute_uids or {route.routeid: min(route.paths, default=0)}
    for subroute_uid, direction in sorted(
        subroute_uids.items(), key=lambda item: (item[1], item[0])
    ):
        connection.execute(
            """
            INSERT OR REPLACE INTO route_subroutes (subroute_uid, routeid, direction)
            VALUES (?, ?, ?)
            """,
            (subroute_uid, route.routeid, direction),
        )

    # RouteUID rows let realtime feeds that stopped populating SubRouteUID
    # (雙北) resolve items back onto this route. Stub routes — shape/stop
    # leftovers with no stops — are skipped so they can never shadow the real
    # route serving the same RouteUID.
    if route.route_uid and any(path.stops for path in route.paths.values()):
        for pathid in sorted(route.paths):
            connection.execute(
                """
                INSERT OR REPLACE INTO route_uids (route_uid, direction, routeid)
                VALUES (?, ?, ?)
                """,
                (route.route_uid, pathid, route.routeid),
            )

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


def _city_temp_db_path(city_db_path: Path) -> Path:
    return city_db_path.with_suffix(f"{city_db_path.suffix}.tmp")


def _main_temp_db_path(db_path: Path) -> Path:
    return db_path.with_suffix(f"{db_path.suffix}.tmp")


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


def _copy_database_versions(source_connection, target_connection) -> None:
    rows = source_connection.execute(
        "SELECT name, version, content_hash, updated_at FROM database_versions ORDER BY name"
    ).fetchall()
    if not rows:
        return
    target_connection.executemany(
        """
        INSERT OR REPLACE INTO database_versions (name, version, content_hash, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                row["name"],
                row["version"],
                row["content_hash"],
                row["updated_at"],
            )
            for row in rows
        ],
    )


def _copy_fetch_state_rows(source_connection, target_connection, where_clause: str = "", parameters: tuple = ()) -> None:
    rows = source_connection.execute(
        f"""
        SELECT resource_key, last_modified, last_status, last_checked_at, last_updated_at
        FROM tdx_fetch_state
        {where_clause}
        ORDER BY resource_key
        """,
        parameters,
    ).fetchall()
    if not rows:
        return
    target_connection.executemany(
        """
        INSERT OR REPLACE INTO tdx_fetch_state
            (resource_key, last_modified, last_status, last_checked_at, last_updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                row["resource_key"],
                row["last_modified"],
                row["last_status"],
                row["last_checked_at"],
                row["last_updated_at"],
            )
            for row in rows
        ],
    )


def _copy_non_static_fetch_state(source_connection, target_connection) -> None:
    # Preserves non-static TDX fetch-state rows (e.g. intercity) across the
    # temp-db rebuild. Application data tables now live in a separate database
    # and are no longer copied here.
    _copy_fetch_state_rows(
        source_connection,
        target_connection,
        "WHERE resource_key NOT LIKE 'static:%'",
    )


def _copy_static_fetch_state_for_city(source_connection, target_connection, city: str) -> None:
    _copy_fetch_state_rows(
        source_connection,
        target_connection,
        "WHERE resource_key LIKE ?",
        (f"static:{city}:%",),
    )


def _copy_main_routes_by_prefix(source_connection, target_connection, prefix: str) -> int:
    route_pattern = f"{prefix}%"
    route_rows = source_connection.execute(
        """
        SELECT routeid, name, name_en
        FROM routes
        WHERE routeid LIKE ?
        ORDER BY routeid
        """,
        (route_pattern,),
    ).fetchall()
    if not route_rows:
        return 0

    target_connection.executemany(
        """
        INSERT OR REPLACE INTO routes (routeid, name, name_en)
        VALUES (?, ?, ?)
        """,
        [
            (
                row["routeid"],
                row["name"],
                row["name_en"],
            )
            for row in route_rows
        ],
    )

    path_rows = source_connection.execute(
        """
        SELECT routeid, pathid, name, name_en
        FROM paths
        WHERE routeid LIKE ?
        ORDER BY routeid, pathid
        """,
        (route_pattern,),
    ).fetchall()
    if path_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO paths (routeid, pathid, name, name_en)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["pathid"],
                    row["name"],
                    row["name_en"],
                )
                for row in path_rows
            ],
        )

    stop_rows = source_connection.execute(
        """
        SELECT routeid, pathid, seq, stopid, name, name_en, lat, lon
        FROM stops
        WHERE routeid LIKE ?
        ORDER BY routeid, pathid, seq
        """,
        (route_pattern,),
    ).fetchall()
    if stop_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO stops
                (routeid, pathid, seq, stopid, name, name_en, lat, lon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["pathid"],
                    row["seq"],
                    row["stopid"],
                    row["name"],
                    row["name_en"],
                    row["lat"],
                    row["lon"],
                )
                for row in stop_rows
            ],
        )

    # Carried forward with everything else: this is the 304 "reuse previous
    # static data" path, and dropping the alias rows here would silently break
    # realtime and stale-routeid resolution for every unchanged city. The source
    # may predate the table on the first run after deploying.
    subroute_rows = (
        source_connection.execute(
            """
            SELECT subroute_uid, routeid, direction
            FROM route_subroutes
            WHERE routeid LIKE ?
            ORDER BY routeid, direction, subroute_uid
            """,
            (route_pattern,),
        ).fetchall()
        if has_route_subroutes_table(source_connection)
        else []
    )
    if subroute_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO route_subroutes (subroute_uid, routeid, direction)
            VALUES (?, ?, ?)
            """,
            [
                (
                    row["subroute_uid"],
                    row["routeid"],
                    row["direction"],
                )
                for row in subroute_rows
            ],
        )

    route_uid_rows = (
        source_connection.execute(
            """
            SELECT route_uid, direction, routeid
            FROM route_uids
            WHERE routeid LIKE ?
            ORDER BY route_uid, direction, routeid
            """,
            (route_pattern,),
        ).fetchall()
        if has_route_uids_table(source_connection)
        else []
    )
    if route_uid_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO route_uids (route_uid, direction, routeid)
            VALUES (?, ?, ?)
            """,
            [
                (
                    row["route_uid"],
                    row["direction"],
                    row["routeid"],
                )
                for row in route_uid_rows
            ],
        )

    point_rows = source_connection.execute(
        """
        SELECT routeid, pathid, seq, lat, lon
        FROM path_points
        WHERE routeid LIKE ?
        ORDER BY routeid, pathid, seq
        """,
        (route_pattern,),
    ).fetchall()
    if point_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO path_points (routeid, pathid, seq, lat, lon)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["pathid"],
                    row["seq"],
                    row["lat"],
                    row["lon"],
                )
                for row in point_rows
            ],
        )

    operator_rows = source_connection.execute(
        """
        SELECT DISTINCT o.operator_id, o.name, o.name_en, o.code, o.phone, o.email, o.url
        FROM operators o
        JOIN route_operators ro ON ro.operator_id = o.operator_id
        WHERE ro.routeid LIKE ?
        ORDER BY o.operator_id
        """,
        (route_pattern,),
    ).fetchall()
    if operator_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO operators
                (operator_id, name, name_en, code, phone, email, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["operator_id"],
                    row["name"],
                    row["name_en"],
                    row["code"],
                    row["phone"],
                    row["email"],
                    row["url"],
                )
                for row in operator_rows
            ],
        )

    route_operator_rows = source_connection.execute(
        """
        SELECT routeid, operator_id, seq
        FROM route_operators
        WHERE routeid LIKE ?
        ORDER BY routeid, seq, operator_id
        """,
        (route_pattern,),
    ).fetchall()
    if route_operator_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO route_operators (routeid, operator_id, seq)
            VALUES (?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["operator_id"],
                    row["seq"],
                )
                for row in route_operator_rows
            ],
        )

    schedule_rows = source_connection.execute(
        """
        SELECT routeid, subroute_uid, direction, kind, seq, service_days, payload
        FROM route_schedules
        WHERE routeid LIKE ?
        ORDER BY routeid, subroute_uid, direction, kind, seq
        """,
        (route_pattern,),
    ).fetchall()
    if schedule_rows:
        target_connection.executemany(
            """
            INSERT OR REPLACE INTO route_schedules
                (routeid, subroute_uid, direction, kind, seq, service_days, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["subroute_uid"],
                    row["direction"],
                    row["kind"],
                    row["seq"],
                    row["service_days"],
                    row["payload"],
                )
                for row in schedule_rows
            ],
        )

    return len(route_rows)


def _write_city_db_from_main(connection, city_db_path: Path, prefix: str) -> None:
    temp_city_db_path = _city_temp_db_path(city_db_path)
    if temp_city_db_path.exists():
        temp_city_db_path.unlink()
    init_city_db(temp_city_db_path)

    stop_rows = connection.execute(
        """
        SELECT routeid, pathid, seq, stopid, name, name_en, lat, lon
        FROM stops
        WHERE routeid LIKE ?
        ORDER BY routeid, pathid, seq
        """,
        (f"{prefix}%",),
    ).fetchall()

    with get_connection(temp_city_db_path) as city_connection:
        with city_connection:
            clear_city_db(city_connection)
            if stop_rows:
                city_connection.executemany(
                    """
                    INSERT OR REPLACE INTO stops
                        (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["routeid"],
                            row["pathid"],
                            row["seq"],
                            row["stopid"],
                            row["name"],
                            row["name_en"],
                            row["lat"],
                            row["lon"],
                        )
                        for row in stop_rows
                    ],
                )

    city_db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_city_db_path.replace(city_db_path)


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
    routeid_mapper: Callable[[str], str] | None = None,
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
                mapped_routeid = routeid_mapper(sub_uid) if routeid_mapper else sub_uid
                if mapped_routeid not in static_route_ids:
                    continue
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {table_name}
                        (routeid, operator_id, seq)
                    VALUES (?, ?, ?)
                    """,
                    (mapped_routeid, op_id, seq),
                )


def _sync_route_schedules(
    connection,
    schedules_raw: list[dict],
    static_route_ids: set[str],
    *,
    table_name: str,
    routeid_mapper: Callable[[str], str] | None = None,
    subroute_uid_mapper: Callable[[str], str] | None = None,
) -> None:
    for sched in schedules_raw:
        route_uid = sched.get("SubRouteUID") or sched.get("RouteUID")
        if not route_uid:
            continue
        mapped_routeid = routeid_mapper(route_uid) if routeid_mapper else route_uid
        if mapped_routeid not in static_route_ids:
            continue
        direction = int(sched.get("Direction") or 0)
        subroute_uid = sched.get("SubRouteUID") or ""
        # Deliberately NOT alias-collapsed: the primary key is
        # (routeid, subroute_uid, direction, kind, seq), so keeping the original
        # per-direction SubRouteUID is what lets both directions of a merged
        # route keep their own timetable rows.
        mapped_subroute_uid = (
            subroute_uid_mapper(subroute_uid)
            if subroute_uid_mapper and subroute_uid
            else subroute_uid
        )

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
                (mapped_routeid, mapped_subroute_uid, direction, freq_seq, service_days, payload),
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
                (mapped_routeid, mapped_subroute_uid, direction, tt_seq, service_days, payload),
            )


def _compute_and_store_travel_times(
    connection,
    schedules_raw: list[dict],
    static_route_ids: set[str],
    *,
    table_name: str = "stop_travel_times",
    routeid_mapper: Callable[[str], str] | None = None,
) -> None:
    """Derives average inter-stop travel times from timetable StopTimes.

    For each route+direction pair that has timetable entries, the function
    iterates over all StopTimes and computes the time difference (in seconds)
    between consecutive stops.  Multiple timetable entries (different service
    day sets or trips) for the same route+direction are averaged so that the
    result is robust against outlier schedules.

    The output is stored in the *stop_travel_times* table keyed by
    ``(routeid, direction, from_seq, to_seq)``.
    """
    # Accumulate raw travel-time observations: (routeid, direction, from_seq, to_seq) -> [seconds]
    observations: dict[tuple[str, int, int, int], list[float]] = {}

    for sched in schedules_raw:
        route_uid = sched.get("SubRouteUID") or sched.get("RouteUID")
        if not route_uid:
            continue
        mapped_routeid = routeid_mapper(route_uid) if routeid_mapper else route_uid
        if mapped_routeid not in static_route_ids:
            continue

        direction = int(sched.get("Direction") or 0)

        for tt in sched.get("Timetables") or []:
            stop_times = tt.get("StopTimes") or []
            if len(stop_times) < 2:
                continue

            # Parse times and build a (seq, minutes-since-midnight) list.
            parsed: list[tuple[int, float]] = []
            for st in stop_times:
                seq = st.get("StopSequence")
                if seq is None:
                    continue
                time_str = (st.get("DepartureTime") or st.get("ArrivalTime") or "").strip()
                minutes = _time_str_to_minutes(time_str)
                if minutes is None:
                    continue
                parsed.append((int(seq), minutes))

            parsed.sort(key=lambda p: p[0])
            for i in range(len(parsed) - 1):
                from_seq, from_min = parsed[i]
                to_seq, to_min = parsed[i + 1]
                diff = to_min - from_min
                if diff < 0:
                    # Cross-midnight (e.g. 23:50 -> 00:10)
                    diff += 24 * 60
                if diff > 180:
                    # Skip unreasonable gaps (> 3 hours between adjacent stops)
                    continue
                key = (mapped_routeid, direction, from_seq, to_seq)
                observations.setdefault(key, []).append(diff * 60.0)  # store as seconds

    # Compute averages and persist.
    for (routeid, direction, from_seq, to_seq), secs_list in observations.items():
        avg_seconds = sum(secs_list) / len(secs_list)
        connection.execute(
            f"""
            INSERT OR REPLACE INTO {table_name}
                (routeid, direction, from_seq, to_seq, avg_seconds, sample_count, source)
            VALUES (?, ?, ?, ?, ?, ?, 'timetable')
            """,
            (routeid, direction, from_seq, to_seq, avg_seconds, len(secs_list)),
        )


def _time_str_to_minutes(time_str: str) -> float | None:
    """Converts 'HH:MM' (or 'HH:MM:SS') to minutes since midnight."""
    if not time_str:
        return None
    parts = time_str.split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        return hours * 60.0 + minutes
    except (ValueError, IndexError):
        return None


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

    main_db_path = settings.db_path
    had_existing_main = main_db_path.exists()
    if had_existing_main:
        init_db(main_db_path)

    temp_main_db_path = _main_temp_db_path(main_db_path)
    if temp_main_db_path.exists():
        temp_main_db_path.unlink()
    init_db(temp_main_db_path)

    effective_cities = cities or settings.tdx_cities
    token_manager = TDXTokenManager(settings)
    client = TDXClient(settings, token_manager)

    try:
        with ExitStack() as stack:
            source_connection = (
                stack.enter_context(get_connection(main_db_path))
                if had_existing_main
                else None
            )
            connection = stack.enter_context(get_connection(temp_main_db_path))

            if source_connection is not None:
                with connection:
                    _copy_non_static_fetch_state(source_connection, connection)
                    _copy_database_versions(source_connection, connection)

            # Which routeid survived each merge last time. Reused so that a
            # group gaining a new direction does not flip its canonical id and
            # invalidate a second wave of saved favorites.
            previous_canonical = (
                load_route_subroute_map(source_connection)
                if source_connection is not None
                else {}
            )
            merge_directions = settings.static_merge_directions

            processed_cities: set[str] = set()
            processed_prefixes: set[str] = {INTERCITY_PREFIX}

            for city in effective_cities:
                LOGGER.info("syncing city=%s", city)
                processed_cities.add(city)

                routes_state = None if source_connection is None else load_tdx_fetch_state(
                    source_connection,
                    _resource_key(city, "routes"),
                )
                stops_state = None if source_connection is None else load_tdx_fetch_state(
                    source_connection,
                    _resource_key(city, "stop_of_route"),
                )
                shapes_state = None if source_connection is None else load_tdx_fetch_state(
                    source_connection,
                    _resource_key(city, "shapes"),
                )

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
                    prefix = CITY_NAME_TO_PREFIX.get(city)
                    copied_routes = 0
                    if prefix and source_connection is not None:
                        processed_prefixes.add(prefix)
                        with connection:
                            copied_routes = _copy_main_routes_by_prefix(
                                source_connection,
                                connection,
                                prefix,
                            )
                    if prefix and copied_routes > 0:
                        _write_city_db_from_main(
                            connection,
                            settings.city_db_path(city),
                            prefix,
                        )
                        LOGGER.info(
                            "city=%s reused previous static data after 304",
                            city,
                        )
                        continue

                    LOGGER.warning(
                        "city=%s returned 304 but no previous rows were available; forcing full refetch",
                        city,
                    )
                    routes_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/Route/City/{city}",
                        if_modified_since=None,
                    )
                    stops_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/StopOfRoute/City/{city}",
                        if_modified_since=None,
                    )
                    shapes_response = client.fetch_paginated_items_conditional(
                        f"/v2/Bus/Shape/City/{city}",
                        if_modified_since=None,
                    )
                    checked_at = int(time.time())
                    _persist_fetch_state(connection, city, "routes", routes_response, checked_at, routes_state)
                    _persist_fetch_state(connection, city, "stop_of_route", stops_response, checked_at, stops_state)
                    _persist_fetch_state(connection, city, "shapes", shapes_response, checked_at, shapes_state)

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
                static_routes = _build_static_routes(
                    routes,
                    stop_of_route_items,
                    shapes,
                    merge_directions=merge_directions,
                    previous_canonical=previous_canonical,
                )
                # SubRouteUID -> surviving routeid, identity included. Every
                # downstream sync must go through this or the rows belonging to
                # an absorbed direction are silently dropped.
                route_alias_map = _alias_map_from_routes(static_routes)

                def _city_routeid_mapper(
                    subroute_uid: str,
                    _alias_map: dict[str, str] = route_alias_map,
                ) -> str:
                    return _alias_map.get(subroute_uid, subroute_uid)

                prefix = CITY_NAME_TO_PREFIX.get(city)
                if prefix:
                    processed_prefixes.add(prefix)
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
                            routeid_mapper=_city_routeid_mapper,
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
                            routeid_mapper=_city_routeid_mapper,
                        )
                        connection.execute(
                            "DELETE FROM stop_travel_times WHERE routeid LIKE ? AND source = 'timetable'",
                            (f"{prefix}%",),
                        )
                        _compute_and_store_travel_times(
                            connection,
                            schedules_raw,
                            set(static_routes),
                            table_name="stop_travel_times",
                            routeid_mapper=_city_routeid_mapper,
                        )

                if prefix:
                    _write_city_db_from_main(
                        connection,
                        settings.city_db_path(city),
                        prefix,
                    )
                city_db_path = settings.city_db_path(city)

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

            inter_name = INTERCITY_CITY_NAME
            LOGGER.info("syncing intercity")

            inter_routes_state = None if source_connection is None else load_tdx_fetch_state(
                source_connection,
                _resource_key(inter_name, "routes"),
            )
            inter_stops_state = None if source_connection is None else load_tdx_fetch_state(
                source_connection,
                _resource_key(inter_name, "stop_of_route"),
            )
            inter_shapes_state = None if source_connection is None else load_tdx_fetch_state(
                source_connection,
                _resource_key(inter_name, "shapes"),
            )

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
                copied_routes = 0
                if source_connection is not None:
                    with connection:
                        copied_routes = _copy_main_routes_by_prefix(
                            source_connection,
                            connection,
                            INTERCITY_PREFIX,
                        )
                if copied_routes > 0:
                    LOGGER.info("intercity reused previous static data after 304")
                else:
                    LOGGER.warning(
                        "intercity returned 304 but no previous rows were available; forcing full refetch",
                    )
                    inter_routes_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/Route/InterCity",
                        if_modified_since=None,
                    )
                    inter_stops_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/StopOfRoute/InterCity",
                        if_modified_since=None,
                    )
                    inter_shapes_response = client.fetch_paginated_items_conditional(
                        "/v2/Bus/Shape/InterCity",
                        if_modified_since=None,
                    )
                    checked_at = int(time.time())
                    _persist_fetch_state(connection, inter_name, "routes", inter_routes_response, checked_at, inter_routes_state)
                    _persist_fetch_state(connection, inter_name, "stop_of_route", inter_stops_response, checked_at, inter_stops_state)
                    _persist_fetch_state(connection, inter_name, "shapes", inter_shapes_response, checked_at, inter_shapes_state)

            if not (
                inter_routes_response.not_modified
                and inter_stops_response.not_modified
                and inter_shapes_response.not_modified
            ):
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
                # The intercity build runs in TDX's own namespace, so the
                # remembered canonicals have to have their INT prefix stripped
                # before they can be matched.
                inter_previous_canonical = {
                    from_intercity_routeid(subroute_uid): from_intercity_routeid(canonical)
                    for subroute_uid, canonical in previous_canonical.items()
                    if subroute_uid.startswith(INTERCITY_PREFIX)
                }
                inter_static_routes = _build_static_routes(
                    inter_routes_raw,
                    inter_stop_of_route_items,
                    inter_shapes_raw,
                    merge_directions=merge_directions,
                    previous_canonical=inter_previous_canonical,
                )
                merged_inter_routes = _prefix_inter_routes(inter_static_routes)
                inter_alias_map = _alias_map_from_routes(merged_inter_routes)

                def _inter_routeid_mapper(
                    subroute_uid: str,
                    _alias_map: dict[str, str] = inter_alias_map,
                ) -> str:
                    local = to_intercity_routeid(subroute_uid)
                    return _alias_map.get(local, local)

                inter_operators_raw = client.fetch_paginated_items("/v2/Bus/Operator/InterCity")
                inter_schedules_raw = client.fetch_paginated_items("/v2/Bus/Schedule/InterCity")

                with connection:
                    delete_main_routes_by_prefix(connection, INTERCITY_PREFIX)
                    for route in merged_inter_routes.values():
                        _replace_main_route(connection, route)
                    connection.execute(
                        "DELETE FROM route_operators WHERE routeid LIKE ?",
                        (f"{INTERCITY_PREFIX}%",),
                    )
                    connection.execute(
                        "DELETE FROM route_schedules WHERE routeid LIKE ?",
                        (f"{INTERCITY_PREFIX}%",),
                    )
                    _upsert_operators(connection, inter_operators_raw)
                    _sync_route_operator_links(
                        connection,
                        inter_routes_raw,
                        set(merged_inter_routes),
                        table_name="route_operators",
                        routeid_mapper=_inter_routeid_mapper,
                    )
                    _sync_route_schedules(
                        connection,
                        inter_schedules_raw,
                        set(merged_inter_routes),
                        table_name="route_schedules",
                        routeid_mapper=_inter_routeid_mapper,
                        subroute_uid_mapper=to_intercity_routeid,
                    )
                    connection.execute(
                        "DELETE FROM stop_travel_times WHERE routeid LIKE ? AND source = 'timetable'",
                        (f"{INTERCITY_PREFIX}%",),
                    )
                    _compute_and_store_travel_times(
                        connection,
                        inter_schedules_raw,
                        set(merged_inter_routes),
                        table_name="stop_travel_times",
                        routeid_mapper=_inter_routeid_mapper,
                    )

                LOGGER.info(
                    "intercity merged_routes=%s stop_of_route=%s shapes=%s status(routes/stops/shapes)=%s/%s/%s",
                    len(merged_inter_routes),
                    len(inter_stop_of_route_items),
                    len(inter_shapes_raw),
                    inter_routes_response.status_code,
                    inter_stops_response.status_code,
                    inter_shapes_response.status_code,
                )

            if source_connection is not None:
                remaining_prefix_rows = source_connection.execute(
                    """
                    SELECT DISTINCT SUBSTR(routeid, 1, 3) AS prefix
                    FROM routes
                    ORDER BY prefix
                    """
                ).fetchall()
                with connection:
                    for row in remaining_prefix_rows:
                        prefix = row["prefix"]
                        if not prefix or prefix in processed_prefixes:
                            continue
                        _copy_main_routes_by_prefix(
                            source_connection,
                            connection,
                            prefix,
                        )

                    for city in settings.tdx_cities:
                        if city in processed_cities:
                            continue
                        _copy_static_fetch_state_for_city(
                            source_connection,
                            connection,
                            city,
                        )

        main_db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_main_db_path.replace(main_db_path)

        export_download_db(main_db_path, settings.download_db_path)
        refresh_database_versions(
            main_db_path,
            download_db_path=settings.download_db_path,
            city_db_paths={
                city: settings.city_db_path(city)
                for city in effective_cities
            },
            force=force,
            # Hash exactly here -- this is the authoritative path -- but record
            # fingerprints so the next server start does not repeat the work.
            app_db_path=settings.app_db_path,
        )
        LOGGER.info("built download db at %s", settings.download_db_path)
    finally:
        client.close()
        token_manager.close()
        if temp_main_db_path.exists():
            temp_main_db_path.unlink()


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
