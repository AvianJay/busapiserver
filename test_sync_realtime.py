from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.db import get_connection, init_db
from app.sync_realtime import RealtimeService, RouteBusesService
from app.tdx_client import TDXJSONResponse


class _FakeTDXClient:
    def __init__(self) -> None:
        self.eta_payload_by_route: dict[str, list[dict]] = {}
        self.buses_payload_by_route: dict[str, list[dict]] = {}

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
        payload: list[dict] = []
        for routeid in routeids:
            payload.extend(self.buses_payload_by_route.get(routeid, []))
        return TDXJSONResponse(payload=payload, status_code=200, last_modified=None)

    def fetch_realtime_by_frequency(self, city: str, routeid: str) -> list[dict]:
        return list(self.buses_payload_by_route.get(routeid, []))


class RealtimeBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        self.download_db_path = Path(self.temp_dir.name) / "downloads" / "bus.db"
        self.download_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.app_db_path = Path(self.temp_dir.name) / "app.db"
        init_db(self.db_path)
        self.settings = Settings(
            project_dir=Path(self.temp_dir.name),
            db_path=self.db_path,
            download_db_path=self.download_db_path,
            tdx_client_id="test",
            tdx_client_secret="test",
            tdx_base_url="https://example.invalid",
            tdx_token_url="https://example.invalid/token",
            tdx_cities=("Taichung",),
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
            app_db_path=self.app_db_path,
        )
        self.client = _FakeTDXClient()
        self._seed_route("TXG307")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(self, routeid: str) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, "307", "307"),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, 0, "Outbound", "Outbound"),
                )
                stops = [
                    (routeid, 0, 1, "STOP1", "Stop 1", "Stop 1", 24.1000, 120.6500),
                    (routeid, 0, 2, "STOP2", "Stop 2", "Stop 2", 24.1006, 120.6500),
                    (routeid, 0, 3, "STOP3", "Stop 3", "Stop 3", 24.1012, 120.6500),
                ]
                connection.executemany(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    stops,
                )
                path_points = [
                    (routeid, 0, 1, 24.1000, 120.6500),
                    (routeid, 0, 2, 24.1006, 120.6500),
                    (routeid, 0, 3, 24.1012, 120.6500),
                ]
                connection.executemany(
                    "INSERT INTO path_points (routeid, pathid, seq, lat, lon) VALUES (?, ?, ?, ?, ?)",
                    path_points,
                )
                travel_times = [
                    (routeid, 0, 1, 2, 60.0, 4, "eta"),
                    (routeid, 0, 2, 3, 90.0, 4, "eta"),
                ]
                connection.executemany(
                    """
                    INSERT INTO stop_travel_times
                        (routeid, direction, from_seq, to_seq, avg_seconds, sample_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    travel_times,
                )

    def _build_services(self) -> tuple[RealtimeService, RouteBusesService]:
        route_buses_service = RouteBusesService(self.settings, self.client)
        realtime_service = RealtimeService(
            self.settings,
            self.client,
            route_buses_service=route_buses_service,
        )
        return realtime_service, route_buses_service

    def test_backfills_missing_bus_into_buses_and_etas(self) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP1",
                "EstimateTime": 120,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "AAA-0001",
                "VehicleStopStatus": 0,
            }
        ]
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "AAA-0001",
                "BusPosition": {"PositionLat": 24.1000, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:00+08:00",
            },
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "BBB-0002",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        anchor_stop = stops["STOP2"]
        downstream_stop = stops["STOP3"]

        self.assertEqual(
            anchor_stop["buses"],
            [{"id": "BBB-0002", "type": "normal", "source": "backfill_buses"}],
        )
        self.assertIn(
            {
                "plate": "BBB-0002",
                "eta": 0,
                "is_arriving": True,
                "source": "backfill_buses",
                "estimated": True,
            },
            anchor_stop["etas"],
        )
        self.assertEqual(anchor_stop["eta"], 0)
        self.assertIn(
            {
                "plate": "BBB-0002",
                "eta": 90,
                "is_arriving": False,
                "source": "backfill_buses",
                "estimated": True,
            },
            downstream_stop["etas"],
        )

    def test_does_not_duplicate_native_plate(self) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP2",
                "EstimateTime": 30,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "CCC-0003",
                "VehicleStopStatus": 1,
            }
        ]
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "CCC-0003",
                "BusPosition": {"PositionLat": 24.1006, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stop2 = next(stop for stop in path["stops"] if stop["stopid"] == "STOP2")
        self.assertEqual(stop2["buses"], [{"id": "CCC-0003", "type": "normal", "source": "tdx"}])
        self.assertEqual(len([eta for eta in stop2["etas"] if eta["plate"] == "CCC-0003"]), 1)

    def test_backfills_only_bus_when_travel_time_missing(self) -> None:
        routeid = "TXG307"
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM stop_travel_times WHERE routeid = ?",
                    (routeid,),
                )

        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "DDD-0004",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)
        path = snapshot["paths"][0]
        stop2 = next(stop for stop in path["stops"] if stop["stopid"] == "STOP2")

        self.assertEqual(stop2["buses"], [{"id": "DDD-0004", "type": "normal", "source": "backfill_buses"}])
        self.assertEqual(
            stop2["etas"],
            [
                {
                    "plate": "DDD-0004",
                    "eta": 0,
                    "is_arriving": True,
                    "source": "backfill_buses",
                    "estimated": True,
                }
            ],
        )
        self.assertEqual(stop2["eta"], 0)
        stop_ids = {stop["stopid"] for stop in path["stops"]}
        self.assertNotIn("STOP3", stop_ids)

    def test_skips_bus_that_is_too_far_from_any_stop(self) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "EEE-0005",
                "BusPosition": {"PositionLat": 24.1100, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)
        path = snapshot["paths"][0]

        for stop in path["stops"]:
            self.assertEqual(stop["buses"], [])
            self.assertEqual(stop["etas"], [])
            self.assertIsNone(stop["eta"])


if __name__ == "__main__":
    unittest.main()
