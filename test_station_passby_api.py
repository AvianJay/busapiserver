from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import requests

from app.api.routes import router
from app.db import get_connection, init_db
from app.rate_limit import reset_rate_limit_state
from app.sync_static import _replace_bus_stations


class _FakeRealtimeService:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict] = {}
        self.requested_routeids: list[str] | None = None
        self.error: Exception | None = None

    def get_batch_snapshots(self, routeids: list[str]) -> dict[str, dict]:
        self.requested_routeids = list(routeids)
        if self.error is not None:
            raise self.error
        return {
            routeid: self.snapshots[routeid]
            for routeid in routeids
            if routeid in self.snapshots
        }


class StationPassbyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.service = _FakeRealtimeService()

        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.state.realtime_service = self.service
        app.include_router(router)
        self.client = TestClient(app)

        self._seed_route("TPE0001", "307", "STOP-A", 0, 2)
        self._seed_route("TPE0002", "藍1", "STOP-B", 1, 5)
        self._seed_route("KHH0001", "紅1", "STOP-A", 0, 1)
        with get_connection(self.db_path) as connection:
            with connection:
                # Deliberately supply B before A. Side labels must be generated
                # from stable StopUID/StopID sorting, never feed order.
                _replace_bus_stations(
                    connection,
                    "TPE",
                    [
                        {
                            "StationUID": "TPE-STATION-1",
                            "StationName": {"Zh_tw": "市政府", "En": "City Hall"},
                            "StationPosition": {
                                "PositionLat": 25.04,
                                "PositionLon": 121.56,
                            },
                            "Stops": [
                                {
                                    "StopUID": "TPE-UID-B",
                                    "StopID": "STOP-B",
                                    "StopName": {"Zh_tw": "市政府（松高路）"},
                                    "StopPosition": {
                                        "PositionLat": 25.041,
                                        "PositionLon": 121.561,
                                    },
                                },
                                {
                                    "StopUID": "TPE-UID-A",
                                    "StopID": "STOP-A",
                                    "StopName": {"Zh_tw": "市政府（忠孝東路）"},
                                    "StopPosition": {
                                        "PositionLat": 25.039,
                                        "PositionLon": 121.559,
                                    },
                                },
                            ],
                        }
                    ],
                )
                _replace_bus_stations(
                    connection,
                    "KHH",
                    [
                        {
                            "StationUID": "KHH-STATION-1",
                            "StationName": {"Zh_tw": "高雄站"},
                            "StationPosition": {
                                "PositionLat": 22.63,
                                "PositionLon": 120.30,
                            },
                            "Stops": [
                                {"StopUID": "KHH-UID-A", "StopID": "STOP-A"},
                            ],
                        }
                    ],
                )

        self.service.snapshots = {
            "TPE0001": self._snapshot("TPE0001", 0, "STOP-A", 90),
            "TPE0002": self._snapshot("TPE0002", 1, "STOP-B", 300),
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(
        self,
        routeid: str,
        route_name: str,
        stopid: str,
        pathid: int,
        seq: int,
    ) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, route_name, route_name),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, pathid, "往終點", "Outbound"),
                )
                connection.execute(
                    """
                    INSERT INTO stops
                        (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (routeid, pathid, seq, stopid, "市政府", "City Hall", 25.04, 121.56),
                )

    @staticmethod
    def _snapshot(routeid: str, pathid: int, stopid: str, eta: int) -> dict:
        return {
            "routeid": routeid,
            "paths": [
                {
                    "pathid": pathid,
                    "stops": [
                        {
                            "stopid": stopid,
                            "eta": eta,
                            "message": "",
                            "updated_at": 1000,
                            "buses": [],
                            "etas": [eta],
                        }
                    ],
                }
            ],
        }

    def test_resolve_returns_stable_station_sides_and_eta_routes(self) -> None:
        response = self.client.get(
            "/api/v1/stations/resolve?city=tpe&stopid=STOP-B"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["city"], "TPE")
        self.assertEqual(body["station_id"], "TPE-STATION-1")
        self.assertEqual(body["station_name"], "市政府")
        self.assertEqual([side["label"] for side in body["sides"]], ["A", "B"])
        self.assertEqual(
            [side["side_id"] for side in body["sides"]],
            ["TPE-UID-A", "TPE-UID-B"],
        )
        self.assertEqual(body["sides"][0]["routes"][0]["routeid"], "TPE0001")
        self.assertEqual(body["sides"][0]["routes"][0]["eta"], 90)
        self.assertEqual(body["sides"][1]["routes"][0]["eta"], 300)
        self.assertEqual(self.service.requested_routeids, ["TPE0001", "TPE0002"])

    def test_passby_returns_station_by_stable_id(self) -> None:
        response = self.client.get(
            "/api/v1/stations/TPE-STATION-1/passby?city=TPE"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["station_name_en"], "City Hall")
        self.assertAlmostEqual(body["lat"], 25.04)
        self.assertEqual(body["sides"][0]["direction"], "市政府（忠孝東路）")

    def test_resolve_is_city_scoped_without_name_or_distance_fallback(self) -> None:
        taipei = self.client.get(
            "/api/v1/stations/resolve?city=TPE&stopid=STOP-A"
        )
        kaohsiung = self.client.get(
            "/api/v1/stations/resolve?city=KHH&stopid=STOP-A"
        )
        missing = self.client.get(
            "/api/v1/stations/resolve?city=TPE&stopid=SIMILAR-NAME"
        )

        self.assertEqual(taipei.json()["station_id"], "TPE-STATION-1")
        self.assertEqual(kaohsiung.json()["station_id"], "KHH-STATION-1")
        self.assertEqual(missing.status_code, 404)

    def test_unknown_city_station_and_invalid_id_are_rejected(self) -> None:
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/resolve?city=XXX&stopid=STOP-A"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/NOPE/passby?city=TPE"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/bad%20id/passby?city=TPE"
            ).status_code,
            400,
        )

    def test_upstream_failure_matches_stop_passby_semantics(self) -> None:
        self.service.error = requests.RequestException("temporary")

        response = self.client.get(
            "/api/v1/stations/TPE-STATION-1/passby?city=TPE"
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "TDX upstream request failed.")


if __name__ == "__main__":
    unittest.main()
