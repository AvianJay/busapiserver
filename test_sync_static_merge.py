"""Tests for merging per-direction SubRoutes into a single route.

Some authorities publish each direction of a route as its own SubRouteUID, which
used to surface as two separate routes in search. ``_build_static_routes`` now
merges direction siblings keyed by ``(RouteUID, SubRouteName)``. The fixtures
below are trimmed copies of the real TDX payload shapes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import requests

from app.api.routes import router
from app.config import INTERCITY_CITY_NAME
from app.db import get_connection, init_db
from app.tdx_client import TDXJSONResponse
from app.rate_limit import reset_rate_limit_state
from app.sync_static import (
    StaticPath,
    StaticRoute,
    _alias_map_from_routes,
    _build_static_routes,
    _choose_canonical,
    _copy_main_routes_by_prefix,
    _fetch_bus_stations,
    _prefix_inter_routes,
    _replace_bus_stations,
    _replace_main_route,
    _sync_route_schedules,
    _compute_and_store_travel_times,
)


def _name(zh: str, en: str | None = None) -> dict:
    return {"Zh_tw": zh, "En": en or zh}


def _subroute(subroute_uid: str, subroute_name: str, direction: int) -> dict:
    return {
        "SubRouteUID": subroute_uid,
        "SubRouteID": subroute_uid,
        "SubRouteName": _name(subroute_name),
        "Direction": direction,
    }


def _route(route_uid: str, route_name: str, subroutes: list[dict], **extra) -> dict:
    payload = {
        "RouteUID": route_uid,
        "RouteID": route_uid,
        "RouteName": _name(route_name),
        "DepartureStopNameZh": extra.get("departure", "臺北車站(東三門)"),
        "DestinationStopNameZh": extra.get("destination", "金青中心"),
        "SubRoutes": subroutes,
    }
    return payload


def _stop_of_route(subroute_uid: str, route_uid: str, direction: int, stops: list[str]) -> dict:
    return {
        "SubRouteUID": subroute_uid,
        "RouteUID": route_uid,
        "Direction": direction,
        "Stops": [
            {
                "StopSequence": index,
                "StopID": f"{subroute_uid}-{index}",
                "StopName": _name(stop_name),
                "StopPosition": {"PositionLat": 25.0 + index / 1000, "PositionLon": 121.5},
            }
            for index, stop_name in enumerate(stops, start=1)
        ],
    }


# 公路客運 1815: one RouteUID, seven lettered variants, each split by direction.
# 1815F exists in direction 1 only.
THB_1815_VARIANTS = ["1815", "1815A", "1815B", "1815C", "1815D", "1815E", "1815G"]


def _thb_1815_routes() -> list[dict]:
    subroutes: list[dict] = []
    for variant in THB_1815_VARIANTS:
        suffix = "0" if variant == "1815" else variant[-1]
        subroutes.append(_subroute(f"THB1815{suffix}1", variant, 0))
        subroutes.append(_subroute(f"THB1815{suffix}2", variant, 1))
    subroutes.append(_subroute("THB1815F2", "1815F", 1))
    return [_route("THB1815", "1815", subroutes)]


def _thb_1815_stop_of_route() -> list[dict]:
    items: list[dict] = []
    for variant in THB_1815_VARIANTS:
        suffix = "0" if variant == "1815" else variant[-1]
        items.append(
            _stop_of_route(f"THB1815{suffix}1", "THB1815", 0, ["臺北車站(東三門)", "金青中心"])
        )
        items.append(
            _stop_of_route(f"THB1815{suffix}2", "THB1815", 1, ["金青中心", "臺北車站(東三門)"])
        )
    items.append(_stop_of_route("THB1815F2", "THB1815", 1, ["金青中心", "臺北車站(東三門)"]))
    return items


# 台北 606: a single SubRouteUID that carries both directions. This is the shape
# that already works, and the merge must not touch it.
def _tpe_606_routes() -> list[dict]:
    return [
        _route(
            "TPE11815",
            "606",
            [
                _subroute("TPE118150", "606", 0),
                _subroute("TPE118150", "606", 1),
            ],
            departure="萬芳社區",
            destination="榮總",
        )
    ]


def _tpe_606_stop_of_route() -> list[dict]:
    return [
        _stop_of_route("TPE118150", "TPE11815", 0, ["萬芳社區", "榮總"]),
        _stop_of_route("TPE118150", "TPE11815", 1, ["榮總", "萬芳社區"]),
    ]


class ChooseCanonicalTests(unittest.TestCase):
    def test_lowest_direction_wins(self) -> None:
        members = [("THB181501", 0), ("THB181502", 1)]
        self.assertEqual(_choose_canonical(members, {}), "THB181501")

    def test_lexicographic_tie_break_within_a_direction(self) -> None:
        members = [("THB1815B1", 0), ("THB1815A1", 0)]
        self.assertEqual(_choose_canonical(sorted(members), {}), "THB1815A1")

    def test_previous_canonical_is_kept_when_still_a_member(self) -> None:
        # A group that used to be direction-1-only just gained a direction 0.
        # The surviving routeid must not flip, or saved favorites break again.
        members = [("THB1815F1", 0), ("THB1815F2", 1)]
        previous = {"THB1815F2": "THB1815F2"}
        self.assertEqual(_choose_canonical(members, previous), "THB1815F2")

    def test_stale_previous_canonical_is_ignored(self) -> None:
        members = [("THB181501", 0), ("THB181502", 1)]
        previous = {"THB181501": "THB1815XX"}
        self.assertEqual(_choose_canonical(members, previous), "THB181501")


class BuildStaticRoutesMergeTests(unittest.TestCase):
    def test_intercity_direction_siblings_collapse_into_one_route(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )

        # 15 SubRouteUIDs in, 8 routes out: 7 merged pairs plus the lone 1815F.
        self.assertEqual(len(static_routes), 8)

        main = static_routes["THB181501"]
        self.assertEqual(main.name, "1815")
        self.assertEqual(sorted(main.paths), [0, 1])
        self.assertEqual(main.subroute_uids, {"THB181501": 0, "THB181502": 1})
        self.assertNotIn("THB181502", static_routes)

    def test_lettered_variants_stay_separate(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )

        for variant in ("A", "B", "C", "D", "E", "G"):
            routeid = f"THB1815{variant}1"
            self.assertIn(routeid, static_routes)
            self.assertEqual(static_routes[routeid].name, f"1815{variant}")
            self.assertEqual(sorted(static_routes[routeid].paths), [0, 1])

    def test_single_direction_group_keeps_its_own_routeid(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )

        lone = static_routes["THB1815F2"]
        self.assertEqual(lone.name, "1815F")
        # No synthetic direction 0 is invented for a one-way group.
        self.assertEqual(sorted(lone.paths), [1])
        self.assertEqual(lone.subroute_uids, {"THB1815F2": 1})

    def test_paths_keep_their_direction_as_pathid(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )

        main = static_routes["THB181501"]
        self.assertEqual([stop.name for stop in main.paths[0].stops][0], "臺北車站(東三門)")
        self.assertEqual([stop.name for stop in main.paths[1].stops][0], "金青中心")

    def test_merge_disabled_reproduces_the_unmerged_rows(self) -> None:
        unmerged = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
            merge_directions=False,
        )

        self.assertEqual(len(unmerged), 15)
        self.assertIn("THB181502", unmerged)
        self.assertEqual(sorted(unmerged["THB181501"].paths), [0])


class MetroShapeIsUnaffectedTests(unittest.TestCase):
    """The five metros already publish both directions under one SubRouteUID."""

    def test_merge_is_a_no_op_for_a_subroute_that_spans_directions(self) -> None:
        merged = _build_static_routes(_tpe_606_routes(), _tpe_606_stop_of_route(), [])
        unmerged = _build_static_routes(
            _tpe_606_routes(),
            _tpe_606_stop_of_route(),
            [],
            merge_directions=False,
        )

        self.assertEqual(merged, unmerged)
        self.assertEqual(list(merged), ["TPE118150"])
        self.assertEqual(sorted(merged["TPE118150"].paths), [0, 1])

    def test_orphan_routeuid_row_is_not_absorbed(self) -> None:
        # StopOfRoute rows without a SubRouteUID fall back to the RouteUID and
        # create their own route. That is pre-existing behaviour; make sure the
        # merge does not quietly fold them into the real route.
        stop_of_route = _tpe_606_stop_of_route()
        stop_of_route.append(
            {
                "RouteUID": "TPE11815",
                "Direction": 0,
                "Stops": [],
            }
        )
        static_routes = _build_static_routes(_tpe_606_routes(), stop_of_route, [])
        self.assertEqual(sorted(static_routes), ["TPE11815", "TPE118150"])


class TainanIsExcludedByDataTests(unittest.TestCase):
    """台南 bakes the direction into the SubRouteName, so nothing merges."""

    def _tnn_routes(self) -> list[dict]:
        return [
            _route(
                "TNN10019",
                "19",
                [
                    _subroute("TNN100190", "19路 安平→大灣", 0),
                    _subroute("TNN100191", "19路 大灣→安平", 1),
                    _subroute("TNN100196", "19路 臺南海事→大灣 [延駛]", 0),
                    _subroute("TNN100197", "19路 大灣→臺南海事 [延駛]", 1),
                ],
            )
        ]

    def test_direction_in_the_name_prevents_merging(self) -> None:
        static_routes = _build_static_routes(self._tnn_routes(), [], [])
        self.assertEqual(
            sorted(static_routes),
            ["TNN100190", "TNN100191", "TNN100196", "TNN100197"],
        )


class CircularRouteTests(unittest.TestCase):
    def test_circular_route_stays_alone_and_its_variant_still_merges(self) -> None:
        routes = [
            _route(
                "HSZ0010",
                "藍線1區",
                [
                    _subroute("HSZ001001", "藍線1區", 0),
                    _subroute("HSZ0010A1", "藍線1區A", 0),
                    _subroute("HSZ0010A2", "藍線1區A", 1),
                ],
            )
        ]
        static_routes = _build_static_routes(routes, [], [])

        self.assertEqual(sorted(static_routes), ["HSZ001001", "HSZ0010A1"])
        self.assertEqual(sorted(static_routes["HSZ001001"].paths), [0])
        self.assertEqual(sorted(static_routes["HSZ0010A1"].paths), [0, 1])


class DirectionConflictGuardTests(unittest.TestCase):
    def test_group_with_duplicate_directions_is_left_unmerged(self) -> None:
        routes = [
            _route(
                "XXX0001",
                "1",
                [
                    _subroute("XXX000101", "1", 0),
                    _subroute("XXX000102", "1", 0),
                ],
            )
        ]

        with self.assertLogs("busapi.sync_static", level="WARNING") as captured:
            static_routes = _build_static_routes(routes, [], [])

        self.assertEqual(sorted(static_routes), ["XXX000101", "XXX000102"])
        self.assertTrue(
            any("duplicate directions" in message for message in captured.output),
            captured.output,
        )


class AliasMapTests(unittest.TestCase):
    def test_alias_map_includes_identity_entries(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        alias_map = _alias_map_from_routes(static_routes)

        self.assertEqual(alias_map["THB181501"], "THB181501")
        self.assertEqual(alias_map["THB181502"], "THB181501")
        self.assertEqual(alias_map["THB1815F2"], "THB1815F2")


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, static_routes: dict) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                for route in static_routes.values():
                    _replace_main_route(connection, route)

    def test_route_subroutes_rows_are_written_for_merged_and_lone_routes(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)

        with get_connection(self.db_path) as connection:
            rows = connection.execute(
                "SELECT subroute_uid, routeid, direction FROM route_subroutes ORDER BY subroute_uid"
            ).fetchall()

        mapping = {row["subroute_uid"]: (row["routeid"], row["direction"]) for row in rows}
        self.assertEqual(mapping["THB181501"], ("THB181501", 0))
        self.assertEqual(mapping["THB181502"], ("THB181501", 1))
        # Singletons get an identity row so lookups can stay branch-free.
        self.assertEqual(mapping["THB1815F2"], ("THB1815F2", 1))
        self.assertEqual(len(mapping), 15)

    def test_rewriting_a_route_replaces_its_alias_rows(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)
        self._write(static_routes)

        with get_connection(self.db_path) as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM route_subroutes").fetchone()["n"]
        self.assertEqual(count, 15)

    def test_both_directions_keep_their_schedules_and_travel_times(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)
        alias_map = _alias_map_from_routes(static_routes)

        schedules = [
            {
                "SubRouteUID": subroute_uid,
                "RouteUID": "THB1815",
                "Direction": direction,
                "Timetables": [
                    {
                        "TripID": f"{subroute_uid}-trip",
                        "ServiceDay": {"Monday": 1},
                        "StopTimes": [
                            {"StopSequence": 1, "DepartureTime": "08:00"},
                            {"StopSequence": 2, "DepartureTime": "08:20"},
                        ],
                    }
                ],
            }
            for subroute_uid, direction in (("THB181501", 0), ("THB181502", 1))
        ]

        with get_connection(self.db_path) as connection:
            with connection:
                _sync_route_schedules(
                    connection,
                    schedules,
                    set(static_routes),
                    table_name="route_schedules",
                    routeid_mapper=lambda uid: alias_map.get(uid, uid),
                )
                _compute_and_store_travel_times(
                    connection,
                    schedules,
                    set(static_routes),
                    table_name="stop_travel_times",
                    routeid_mapper=lambda uid: alias_map.get(uid, uid),
                )

            schedule_rows = connection.execute(
                """
                SELECT routeid, subroute_uid, direction
                FROM route_schedules
                WHERE routeid = 'THB181501'
                ORDER BY direction
                """
            ).fetchall()
            travel_rows = connection.execute(
                """
                SELECT direction FROM stop_travel_times
                WHERE routeid = 'THB181501' ORDER BY direction
                """
            ).fetchall()

        # The absorbed direction's rows must survive under the canonical
        # routeid, and keep their own SubRouteUID so the primary key still
        # distinguishes the two directions.
        self.assertEqual(
            [(row["subroute_uid"], row["direction"]) for row in schedule_rows],
            [("THB181501", 0), ("THB181502", 1)],
        )
        self.assertEqual([row["direction"] for row in travel_rows], [0, 1])

    def test_copy_main_routes_by_prefix_round_trips_alias_rows(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)

        target_path = Path(self.temp_dir.name) / "bus.db.tmp"
        init_db(target_path)

        with get_connection(self.db_path) as source, get_connection(target_path) as target:
            with target:
                copied = _copy_main_routes_by_prefix(source, target, "THB")
            rows = target.execute(
                "SELECT subroute_uid, routeid, direction FROM route_subroutes ORDER BY subroute_uid"
            ).fetchall()

        self.assertEqual(copied, 8)
        mapping = {row["subroute_uid"]: row["routeid"] for row in rows}
        self.assertEqual(mapping["THB181502"], "THB181501")
        self.assertEqual(len(mapping), 15)

    def test_copy_main_routes_by_prefix_round_trips_station_rows(self) -> None:
        # When every TDX resource for a city answers 304, sync_static reuses the
        # previous database through this copier instead of refetching. Stations
        # must survive that path: if they do not, /stations/resolve and
        # /stations/{id}/passby start 404-ing for every unchanged city on the
        # second sync, and the app can no longer favourite a whole station.
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)

        with get_connection(self.db_path) as source:
            with source:
                _replace_bus_stations(
                    source,
                    "THB",
                    [
                        {
                            "StationUID": "THB-STATION-1",
                            "StationName": {"Zh_tw": "台中車站", "En": "Taichung"},
                            "StationPosition": {
                                "PositionLat": 24.137,
                                "PositionLon": 120.685,
                            },
                            "Stops": [
                                {
                                    "StopUID": "THB-UID-B",
                                    "StopID": "STOP-B",
                                    "StopName": {"Zh_tw": "台中車站（建國路）"},
                                    "StopPosition": {
                                        "PositionLat": 24.138,
                                        "PositionLon": 120.686,
                                    },
                                },
                                {
                                    "StopUID": "THB-UID-A",
                                    "StopID": "STOP-A",
                                    "StopName": {"Zh_tw": "台中車站（台灣大道）"},
                                    "StopPosition": {
                                        "PositionLat": 24.136,
                                        "PositionLon": 120.684,
                                    },
                                },
                            ],
                        }
                    ],
                )

        target_path = Path(self.temp_dir.name) / "bus.db.tmp"
        init_db(target_path)

        with get_connection(self.db_path) as source, get_connection(target_path) as target:
            with target:
                copied = _copy_main_routes_by_prefix(source, target, "THB")
            stations = target.execute(
                "SELECT city_code, station_id, name FROM stations"
            ).fetchall()
            sides = target.execute(
                "SELECT stop_id, stop_uid, side_order, side_label FROM station_stops "
                "ORDER BY side_order"
            ).fetchall()

        # The route count contract the 304 reuse path branches on is unchanged.
        self.assertEqual(copied, 8)
        self.assertEqual(
            [(row["city_code"], row["station_id"], row["name"]) for row in stations],
            [("THB", "THB-STATION-1", "台中車站")],
        )
        # Sides keep their stable StopUID-sorted order and generated labels, so
        # the copied rows resolve identically to the freshly synced ones.
        self.assertEqual(
            [(row["stop_id"], row["stop_uid"], row["side_order"]) for row in sides],
            [("STOP-A", "THB-UID-A", 0), ("STOP-B", "THB-UID-B", 1)],
        )
        self.assertEqual([row["side_label"] for row in sides], ["A", "B"])

    def test_route_uid_rows_are_written_per_pathid(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)
        self._write(static_routes)  # rewriting must replace, not accumulate

        with get_connection(self.db_path) as connection:
            rows = connection.execute(
                "SELECT route_uid, direction, routeid FROM route_uids ORDER BY routeid, direction"
            ).fetchall()

        triples = {(row["route_uid"], row["direction"], row["routeid"]) for row in rows}
        # The merged main variant covers both directions under one routeid...
        self.assertIn(("THB1815", 0, "THB181501"), triples)
        self.assertIn(("THB1815", 1, "THB181501"), triples)
        # ...and the direction-1-only variant gets just its own direction.
        self.assertIn(("THB1815", 1, "THB1815F2"), triples)
        self.assertNotIn(("THB1815", 0, "THB1815F2"), triples)
        # 7 two-direction routes + 1 single-direction route.
        self.assertEqual(len(rows), 15)

    def test_stub_routes_get_no_route_uid_rows(self) -> None:
        stub = StaticRoute(
            routeid="TPE10132",
            name="TPE10132",
            name_en=None,
            route_uid="TPE10132",
            paths={
                0: StaticPath(pathid=0, name="Unknown", points=[(25.0, 121.5)]),
                1: StaticPath(pathid=1, name="Unknown", points=[(25.1, 121.6)]),
            },
        )
        self._write({stub.routeid: stub})

        with get_connection(self.db_path) as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM route_uids").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_copy_main_routes_by_prefix_round_trips_route_uid_rows(self) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
        )
        self._write(static_routes)

        target_path = Path(self.temp_dir.name) / "bus.db.tmp"
        init_db(target_path)

        with get_connection(self.db_path) as source, get_connection(target_path) as target:
            with target:
                _copy_main_routes_by_prefix(source, target, "THB")
            count = target.execute("SELECT COUNT(*) AS n FROM route_uids").fetchone()["n"]

        self.assertEqual(count, 15)


class StationSidePositionTests(unittest.TestCase):
    """Sides must carry the pole's own position, not the station's.

    TDX's Station feed ships no usable per-stop position, so falling back to
    StationPosition made every station_stops row identical to its parent — all
    154k of them in production. That makes the column useless for telling sides
    apart or collapsing co-located ones. StopOfRoute has the real positions and
    is already in `stops` when _replace_bus_stations runs.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES ('TPE1', '1', '1')"
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en)"
                    " VALUES ('TPE1', 0, '往終點', 'Outbound')"
                )
                # Two poles on opposite kerbs, both well away from the station
                # centroid so a fallback is unmistakable.
                for seq, (stopid, lat, lon) in enumerate(
                    (("STOP-A", 25.0391, 121.5588), ("STOP-B", 25.0414, 121.5613)), start=1
                ):
                    connection.execute(
                        "INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)"
                        " VALUES ('TPE1', 0, ?, ?, '市政府', 'City Hall', ?, ?)",
                        (seq, stopid, lat, lon),
                    )

    def _station(self, *stop_ids: str) -> list[dict]:
        return [
            {
                "StationUID": "TPE-STATION-1",
                "StationName": {"Zh_tw": "市政府"},
                "StationPosition": {"PositionLat": 25.04, "PositionLon": 121.56},
                "Stops": [
                    {"StopUID": f"UID-{s}", "StopID": s, "StopName": {"Zh_tw": "市政府"}}
                    for s in stop_ids
                ],
            }
        ]

    def _sides(self) -> list[tuple]:
        with get_connection(self.db_path) as connection:
            return [
                (row["stop_id"], row["lat"], row["lon"])
                for row in connection.execute(
                    "SELECT stop_id, lat, lon FROM station_stops ORDER BY side_order"
                )
            ]

    def test_sides_take_their_own_stop_position(self) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                _replace_bus_stations(connection, "TPE", self._station("STOP-A", "STOP-B"))

        self.assertEqual(
            self._sides(),
            [("STOP-A", 25.0391, 121.5588), ("STOP-B", 25.0414, 121.5613)],
        )

    def test_sides_are_distinguishable_from_each_other(self) -> None:
        # The property the whole fix exists for: two sides of one station must
        # not collapse onto a single coordinate.
        with get_connection(self.db_path) as connection:
            with connection:
                _replace_bus_stations(connection, "TPE", self._station("STOP-A", "STOP-B"))

        positions = {(lat, lon) for _, lat, lon in self._sides()}
        self.assertEqual(len(positions), 2)
        self.assertNotIn((25.04, 121.56), positions)

    def test_unknown_stop_id_falls_back_to_the_station_position(self) -> None:
        # 0.1% of production sides have no matching stops row; they must still
        # land somewhere sane rather than at (0, 0).
        with get_connection(self.db_path) as connection:
            with connection:
                _replace_bus_stations(connection, "TPE", self._station("STOP-A", "STOP-GHOST"))

        self.assertEqual(
            self._sides(),
            [("STOP-A", 25.0391, 121.5588), ("STOP-GHOST", 25.04, 121.56)],
        )


class FetchBusStationsTests(unittest.TestCase):
    """TDX does not expose a Station feed for every authority.

    ``/v2/Bus/Station/City/LienchiangCounty`` answers 400. Because sync_static
    builds into a temp database and only swaps it onto bus.db at the very end,
    letting that 400 escape discards every city synced before it — a whole run
    lost to one authority that never had stations to begin with.
    """

    class _Client:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.paths: list[str] = []

        def fetch_paginated_items_conditional(self, path, *, if_modified_since=None):
            self.paths.append(path)
            if self.error is not None:
                raise self.error
            return TDXJSONResponse(payload=[{"StationUID": "X"}], status_code=200, last_modified=None)

    @staticmethod
    def _http_error(status: int) -> requests.HTTPError:
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(f"{status} Client Error", response=response)

    def test_missing_station_feed_degrades_to_none(self) -> None:
        client = self._Client(self._http_error(400))

        result = _fetch_bus_stations(client, "LienchiangCounty", if_modified_since=None)

        self.assertIsNone(result)
        self.assertEqual(client.paths, ["/v2/Bus/Station/City/LienchiangCounty"])

    def test_transient_failures_still_raise(self) -> None:
        # 429 and 5xx are retried inside the client; if they reach here the run
        # should fail loudly rather than silently drop a city's stations.
        for status in (429, 500, 503):
            with self.subTest(status=status):
                client = self._Client(self._http_error(status))
                with self.assertRaises(requests.HTTPError):
                    _fetch_bus_stations(client, "Taichung", if_modified_since=None)

    def test_intercity_uses_its_own_endpoint(self) -> None:
        client = self._Client()

        result = _fetch_bus_stations(client, INTERCITY_CITY_NAME, if_modified_since=None)

        self.assertIsNotNone(result)
        self.assertEqual(client.paths, ["/v2/Bus/Station/InterCity"])

    def test_present_feed_is_returned_unchanged(self) -> None:
        client = self._Client()

        result = _fetch_bus_stations(client, "Taichung", if_modified_since="Mon, 01 Sep 2026 00:00:00 GMT")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.payload, [{"StationUID": "X"}])
        self.assertEqual(client.paths, ["/v2/Bus/Station/City/Taichung"])


class SearchResultTests(unittest.TestCase):
    """The user-visible symptom: one card, not two."""

    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)

        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed(self, *, merge_directions: bool) -> None:
        static_routes = _build_static_routes(
            _thb_1815_routes(),
            _thb_1815_stop_of_route(),
            [],
            merge_directions=merge_directions,
        )
        with get_connection(self.db_path) as connection:
            with connection:
                for route in _prefix_inter_routes(static_routes).values():
                    _replace_main_route(connection, route)

    def test_merged_route_is_a_single_search_result_with_both_terminals(self) -> None:
        self._seed(merge_directions=True)

        rows = self.client.get("/api/v1/routes?query=1815&limit=50").json()

        exact = [row for row in rows if row["route_name"] == "1815"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["routeid"], "INTTHB181501")
        # Same rendering the six metros already get for free.
        self.assertEqual(exact[0]["path_name"], "金青中心 / 臺北車站(東三門)")
        self.assertEqual(exact[0]["city_code"], "INT")
        # 15 SubRouteUIDs collapse to 8 search results.
        self.assertEqual(len(rows), 8)

    def test_without_the_merge_the_duplicate_is_reproduced(self) -> None:
        self._seed(merge_directions=False)

        rows = self.client.get("/api/v1/routes?query=1815&limit=50").json()

        exact = [row for row in rows if row["route_name"] == "1815"]
        self.assertEqual(len(exact), 2)
        self.assertEqual(len(rows), 15)


if __name__ == "__main__":
    unittest.main()
