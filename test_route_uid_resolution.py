"""Tests for resolving RouteUID-only realtime items (雙北 feeds).

雙北 realtime feeds stopped populating SubRouteUID: items carry only RouteUID +
Direction. Outbound requests for those cities must filter on RouteUID, and
inbound items must resolve through the route_uids table (or, on databases that
predate it, a naming-convention derivation) back onto local routeids.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from app.config import Settings
from app.db import get_connection, init_db
from app.route_aliases import get_route_alias_index, reset_route_alias_cache
from app.sync_realtime import RealtimeService, RouteBusesService, _tdx_item_to_local
from app.tdx_client import TDXClient, TDXJSONResponse


def _settings(temp_dir: str, db_path: Path, **overrides) -> Settings:
    kwargs = dict(
        project_dir=Path(temp_dir),
        db_path=db_path,
        download_db_path=Path(temp_dir) / "downloads" / "bus.db",
        tdx_client_id="test",
        tdx_client_secret="test",
        tdx_base_url="https://example.invalid",
        tdx_token_url="https://example.invalid/token",
        tdx_cities=("Taipei", "NewTaipei", "Taichung"),
        tdx_request_timeout=30,
        tdx_token_refresh_skew=300,
        tdx_retry_attempts=1,
        tdx_retry_backoff=1.0,
        tdx_min_request_interval=0.0,
        realtime_cache_ttl=5,
        realtime_track_ttl=30,
        cors_origins=(),
        auth_public_base_url="https://bus.example.invalid",
        auth_state_ttl_seconds=600,
        auth_snowflake_node_id=0,
        discord_oauth_client_id=None,
        discord_oauth_client_secret=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_native_oauth_client_ids=(),
        app_db_path=Path(temp_dir) / "app.db",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _seed_route(
    db_path: Path,
    routeid: str,
    name: str,
    stops_by_path: dict[int, list[tuple[int, str, str]]],
) -> None:
    """stops_by_path: pathid -> [(seq, stopid, stop_name)]. Empty dict = stub."""
    with get_connection(db_path) as connection:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                (routeid, name, name),
            )
            for pathid, stops in stops_by_path.items():
                connection.execute(
                    "INSERT OR IGNORE INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, pathid, f"path{pathid}", f"path{pathid}"),
                )
                connection.executemany(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (routeid, pathid, seq, stopid, stop_name, stop_name, 25.0, 121.5)
                        for seq, stopid, stop_name in stops
                    ],
                )


def _seed_stub(db_path: Path, routeid: str, pathids: tuple[int, ...] = (0, 1)) -> None:
    """A RouteUID-keyed leftover: name == routeid, paths but no stops."""
    with get_connection(db_path) as connection:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO routes (routeid, name, name_en) VALUES (?, ?, NULL)",
                (routeid, routeid),
            )
            for pathid in pathids:
                connection.execute(
                    "INSERT OR IGNORE INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, 'Unknown', NULL)",
                    (routeid, pathid),
                )


def _seed_route_uid_rows(db_path: Path, rows: list[tuple[str, int, str]]) -> None:
    with get_connection(db_path) as connection:
        with connection:
            connection.executemany(
                "INSERT OR REPLACE INTO route_uids (route_uid, direction, routeid) VALUES (?, ?, ?)",
                rows,
            )


def _seed_taipei_234(db_path: Path) -> None:
    """The real shape: TPE101320 carries both directions, TPE10132 is the stub."""
    _seed_route(
        db_path,
        "TPE101320",
        "234",
        {
            0: [(1, "33214", "西門"), (2, "33215", "貴陽州街口")],
            1: [(1, "33215", "貴陽州街口"), (2, "33214", "西門")],
        },
    )
    _seed_stub(db_path, "TPE10132")


class _IndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(self.temp_dir.name, self.db_path)

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def _index(self):
        reset_route_alias_cache()
        return get_route_alias_index(self.settings)


class RouteUidIndexTests(_IndexTestCase):
    """Lookups backed by persisted route_uids rows."""

    def test_unique_route_uid_resolves_both_ways(self) -> None:
        _seed_taipei_234(self.db_path)
        _seed_route_uid_rows(
            self.db_path,
            [("TPE10132", 0, "TPE101320"), ("TPE10132", 1, "TPE101320")],
        )

        index = self._index()
        self.assertEqual(index.routeid_for_route_uid("TPE10132"), "TPE101320")
        self.assertEqual(index.routeid_for_route_uid("TPE10132", 1), "TPE101320")
        self.assertEqual(index.route_uid_for("TPE101320"), "TPE10132")

    def test_shared_route_uid_fails_closed(self) -> None:
        # 區間 variants: one RouteUID, several distinct routes. A feed that
        # only tags RouteUID cannot say which variant an item belongs to.
        _seed_route(self.db_path, "TPE157750", "617", {0: [(1, "100", "A")]})
        _seed_route(self.db_path, "TPE157751", "617區", {0: [(1, "100", "A")]})
        _seed_route_uid_rows(
            self.db_path,
            [("TPE15775", 0, "TPE157750"), ("TPE15775", 0, "TPE157751")],
        )

        index = self._index()
        self.assertIsNone(index.routeid_for_route_uid("TPE15775"))
        self.assertIsNone(index.routeid_for_route_uid("TPE15775", 0))
        # Outbound still works: both variants query by the shared RouteUID.
        self.assertEqual(index.route_uid_for("TPE157750"), "TPE15775")
        self.assertEqual(index.route_uid_for("TPE157751"), "TPE15775")

    def test_direction_disambiguates_when_uid_level_cannot(self) -> None:
        # Two unmerged per-direction routes sharing a RouteUID: ambiguous at
        # uid level, resolvable per direction.
        _seed_route(self.db_path, "NWT100010", "801去", {0: [(1, "200", "B")]})
        _seed_route(self.db_path, "NWT100011", "801回", {1: [(1, "201", "C")]})
        _seed_route_uid_rows(
            self.db_path,
            [("NWT10001", 0, "NWT100010"), ("NWT10001", 1, "NWT100011")],
        )

        index = self._index()
        self.assertIsNone(index.routeid_for_route_uid("NWT10001"))
        self.assertEqual(index.routeid_for_route_uid("NWT10001", 0), "NWT100010")
        self.assertEqual(index.routeid_for_route_uid("NWT10001", 1), "NWT100011")


class LegacyDerivationTests(_IndexTestCase):
    """Databases that predate route_uids derive the maps from naming."""

    def test_stub_evidenced_trim_resolves(self) -> None:
        _seed_taipei_234(self.db_path)

        index = self._index()
        self.assertEqual(index.routeid_for_route_uid("TPE10132"), "TPE101320")
        self.assertEqual(index.routeid_for_route_uid("TPE10132", 1), "TPE101320")
        self.assertEqual(index.route_uid_for("TPE101320"), "TPE10132")

    def test_identity_routeid_maps_to_itself(self) -> None:
        # No trailing-digit sibling and no stub: the routeid IS the RouteUID.
        _seed_route(self.db_path, "TPE10142", "南環幹線", {0: [(1, "300", "D")]})

        index = self._index()
        self.assertEqual(index.routeid_for_route_uid("TPE10142"), "TPE10142")
        self.assertEqual(index.route_uid_for("TPE10142"), "TPE10142")

    def test_variant_family_fails_closed(self) -> None:
        for suffix in ("0", "1", "2"):
            _seed_route(
                self.db_path,
                f"TPE15775{suffix}",
                f"617-{suffix}",
                {0: [(1, "400", "E")]},
            )

        index = self._index()
        self.assertIsNone(index.routeid_for_route_uid("TPE15775"))
        # No stub proves the trimmed id, so each variant queries by itself.
        self.assertEqual(index.route_uid_for("TPE157750"), "TPE157750")

    def test_stub_is_never_a_resolution_target(self) -> None:
        _seed_taipei_234(self.db_path)

        index = self._index()
        self.assertEqual(index.routeid_for_route_uid("TPE10132"), "TPE101320")
        self.assertIsNone(index.route_uid_for("TPE10132"))

    def test_non_legacy_prefixes_are_not_derived(self) -> None:
        _seed_route(self.db_path, "TXG9221", "922", {0: [(1, "500", "F")]})

        index = self._index()
        self.assertIsNone(index.route_uid_for("TXG9221"))
        self.assertIsNone(index.routeid_for_route_uid("TXG922"))

    def test_persisted_rows_disable_derivation_for_the_prefix(self) -> None:
        _seed_taipei_234(self.db_path)
        _seed_route(self.db_path, "TPE20001", "999", {0: [(1, "600", "G")]})
        # One persisted TPE row means the table owns the prefix; the stub trim
        # for TPE101320 must NOT be derived on top of it.
        _seed_route_uid_rows(self.db_path, [("TPE20001", 0, "TPE20001")])

        index = self._index()
        self.assertEqual(index.routeid_for_route_uid("TPE20001"), "TPE20001")
        self.assertIsNone(index.routeid_for_route_uid("TPE10132"))
        self.assertIsNone(index.route_uid_for("TPE101320"))


class TdxItemToLocalTests(_IndexTestCase):
    def test_subroute_uid_wins_when_present(self) -> None:
        _seed_taipei_234(self.db_path)
        reset_route_alias_cache()

        item = {"RouteUID": "TPE10132", "SubRouteUID": "TPE101320", "Direction": 0}
        self.assertEqual(
            _tdx_item_to_local("Taipei", item, settings=self.settings),
            "TPE101320",
        )

    def test_null_subroute_uid_resolves_via_route_uid(self) -> None:
        _seed_taipei_234(self.db_path)
        reset_route_alias_cache()

        for missing in (None, ""):
            item = {"RouteUID": "TPE10132", "SubRouteUID": missing, "Direction": 1}
            self.assertEqual(
                _tdx_item_to_local("Taipei", item, settings=self.settings),
                "TPE101320",
            )

    def test_unmapped_route_uid_falls_back_to_itself(self) -> None:
        reset_route_alias_cache()

        item = {"RouteUID": "TPE99999", "SubRouteUID": None, "Direction": 0}
        # No mapping: the RouteUID passes through as a routeid candidate, which
        # the caller's static-route membership check then drops.
        self.assertEqual(
            _tdx_item_to_local("Taipei", item, settings=self.settings),
            "TPE99999",
        )

    def test_item_without_any_id_is_none(self) -> None:
        self.assertIsNone(
            _tdx_item_to_local("Taipei", {"Direction": 0}, settings=self.settings)
        )


class TdxRouteUidOutboundTests(_IndexTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TDXClient(self.settings, token_manager=None)

    def test_filter_city_selection_follows_settings(self) -> None:
        self.assertTrue(self.client._uses_routeuid_filter("Taipei"))
        self.assertTrue(self.client._uses_routeuid_filter("NewTaipei"))
        self.assertFalse(self.client._uses_routeuid_filter("Taichung"))
        self.assertFalse(self.client._uses_routeuid_filter("InterCity"))

    def test_normalize_route_uids_maps_and_dedupes(self) -> None:
        _seed_taipei_234(self.db_path)
        _seed_route(self.db_path, "TPE10142", "南環幹線", {0: [(1, "300", "D")]})
        reset_route_alias_cache()

        normalized = self.client._normalize_route_uids_for_city(
            "Taipei", ["TPE101320", "TPE10142", "TPE101320"]
        )
        self.assertEqual(normalized, ["TPE10132", "TPE10142"])

    def test_variant_family_collapses_to_one_route_uid(self) -> None:
        _seed_route(self.db_path, "TPE157750", "617", {0: [(1, "100", "A")]})
        _seed_route(self.db_path, "TPE157751", "617區", {0: [(1, "100", "A")]})
        _seed_route_uid_rows(
            self.db_path,
            [("TPE15775", 0, "TPE157750"), ("TPE15775", 0, "TPE157751")],
        )
        reset_route_alias_cache()

        normalized = self.client._normalize_route_uids_for_city(
            "Taipei", ["TPE157750", "TPE157751"]
        )
        self.assertEqual(normalized, ["TPE15775"])

    def _capture_requests(self):
        captured: list[dict] = []

        def fake_request(path, params=None, if_modified_since=None, allow_not_modified=False):
            captured.append(dict(params or {}))
            return SimpleNamespace(
                payload=[],
                status_code=200,
                last_modified=None,
                not_modified=False,
            )

        self.client._request_json_with_meta = fake_request
        return captured

    def test_taipei_eta_batch_filters_on_route_uid(self) -> None:
        _seed_taipei_234(self.db_path)
        reset_route_alias_cache()
        captured = self._capture_requests()

        self.client.fetch_estimated_time_of_arrival_batch("Taipei", ["TPE101320"])

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["$filter"], "RouteUID eq 'TPE10132'")

    def test_taipei_buses_batch_filters_on_route_uid(self) -> None:
        _seed_taipei_234(self.db_path)
        reset_route_alias_cache()
        captured = self._capture_requests()

        self.client.fetch_realtime_by_frequency_batch("Taipei", ["TPE101320"])

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["$filter"], "RouteUID eq 'TPE10132'")

    def test_other_cities_keep_the_subroute_uid_filter(self) -> None:
        captured = self._capture_requests()

        self.client.fetch_estimated_time_of_arrival_batch("Taichung", ["TXG1"])

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["$filter"], "SubRouteUID eq 'TXG1'")

    def test_empty_filter_city_list_restores_legacy_behaviour(self) -> None:
        settings = _settings(
            self.temp_dir.name,
            self.db_path,
            tdx_routeuid_filter_cities=(),
        )
        client = TDXClient(settings, token_manager=None)
        self.assertFalse(client._uses_routeuid_filter("Taipei"))


class _FakeTDXClient:
    """Returns canned ETA payloads keyed by the locally requested routeid."""

    def __init__(self) -> None:
        self.eta_payload_by_route: dict[str, list[dict]] = {}

    def fetch_estimated_time_of_arrival_batch(
        self,
        city: str,
        routeids: list[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        payload: list[dict] = []
        for routeid in routeids:
            payload.extend(self.eta_payload_by_route.get(routeid, []))
        return TDXJSONResponse(payload=payload, status_code=200, last_modified=None)

    def fetch_estimated_time_of_arrival(self, city: str, routeid: str) -> list[dict]:
        return list(self.eta_payload_by_route.get(routeid, []))

    def fetch_realtime_by_frequency_batch(
        self,
        city: str,
        routeids: list[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        return TDXJSONResponse(payload=[], status_code=200, last_modified=None)

    def fetch_realtime_by_frequency(self, city: str, routeid: str) -> list[dict]:
        return []


class NullSubRouteUidRealtimeTests(unittest.TestCase):
    """End to end: RouteUID-only items land on the right path and stop."""

    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(self.temp_dir.name, self.db_path)
        self.client = _FakeTDXClient()
        route_buses_service = RouteBusesService(self.settings, self.client)
        self.service = RealtimeService(
            self.settings,
            self.client,
            route_buses_service=route_buses_service,
        )

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    @staticmethod
    def _eta_item(route_uid: str, direction: int, stopid: str, eta: int) -> dict:
        return {
            "RouteUID": route_uid,
            "SubRouteUID": None,
            "SubRouteID": None,
            "Direction": direction,
            "StopID": stopid,
            "EstimateTime": eta,
            "StopStatus": 0,
        }

    def test_route_uid_only_items_bucket_onto_the_route(self) -> None:
        # Legacy DB: no route_uids rows, resolution relies on derivation.
        _seed_taipei_234(self.db_path)
        reset_route_alias_cache()
        self.client.eta_payload_by_route["TPE101320"] = [
            self._eta_item("TPE10132", 0, "33215", 120),
            self._eta_item("TPE10132", 1, "33215", 480),
        ]

        snapshot = self.service.get_snapshot("TPE101320")

        paths = {path["pathid"]: path for path in snapshot["paths"]}
        self.assertEqual(sorted(paths), [0, 1])
        outbound = next(s for s in paths[0]["stops"] if s["stopid"] == "33215")
        inbound = next(s for s in paths[1]["stops"] if s["stopid"] == "33215")
        self.assertEqual(outbound["eta"], 120)
        self.assertEqual(inbound["eta"], 480)

    def test_ambiguous_route_uid_items_are_dropped_not_guessed(self) -> None:
        _seed_route(self.db_path, "TPE157750", "617", {0: [(1, "100", "A")]})
        _seed_route(self.db_path, "TPE157751", "617區", {0: [(1, "100", "A")]})
        _seed_route_uid_rows(
            self.db_path,
            [("TPE15775", 0, "TPE157750"), ("TPE15775", 0, "TPE157751")],
        )
        reset_route_alias_cache()
        self.client.eta_payload_by_route["TPE157750"] = [
            self._eta_item("TPE15775", 0, "100", 60),
        ]

        # Neither variant may claim the ambiguous item's ETA.
        for routeid in ("TPE157750", "TPE157751"):
            snapshot = self.service.get_snapshot(routeid)
            etas = [
                stop.get("eta")
                for path in snapshot["paths"]
                for stop in path["stops"]
            ]
            self.assertNotIn(60, etas)


if __name__ == "__main__":
    unittest.main()
