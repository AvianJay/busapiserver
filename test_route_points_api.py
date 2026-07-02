from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import _encode_polyline, router
from app.db import get_connection, init_db
from app.rate_limit import reset_rate_limit_state


class RoutePathPointsApiTests(unittest.TestCase):
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

    def _seed_route(
        self,
        routeid: str,
        *,
        path_points: list[tuple[int, float, float]] | None = None,
    ) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, "57", "57"),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, 0, "Outbound", "Outbound"),
                )
                connection.executemany(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (routeid, 0, 1, "STOP1", "Stop 1", "Stop 1", 25.0123, 121.4567),
                        (routeid, 0, 2, "STOP2", "Stop 2", "Stop 2", 25.0345, 121.4789),
                        (routeid, 0, 3, "STOP3", "Stop 3", "Stop 3", 25.0567, 121.5012),
                    ],
                )
                if path_points:
                    connection.executemany(
                        """
                        INSERT INTO path_points (routeid, pathid, seq, lat, lon)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (routeid, 0, seq, lat, lon)
                            for seq, lat, lon in path_points
                        ],
                    )

    def test_path_points_fall_back_to_static_stops_when_geometry_is_missing(self) -> None:
        routeid = "NWT157491"
        self._seed_route(routeid)

        response = self.client.get(f"/api/v1/routes/{routeid}/paths/0/points")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "routeid": routeid,
                "pathid": 0,
                "polyline": _encode_polyline(
                    [
                        (25.0123, 121.4567),
                        (25.0345, 121.4789),
                        (25.0567, 121.5012),
                    ]
                ),
            },
        )

    def test_existing_path_points_are_preferred_over_stop_fallback(self) -> None:
        routeid = "NWT157492"
        geometry = [
            (1, 25.0001, 121.4001),
            (2, 25.0002, 121.4002),
        ]
        self._seed_route(routeid, path_points=geometry)

        response = self.client.get(f"/api/v1/routes/{routeid}/paths/0/points")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["polyline"],
            _encode_polyline([(lat, lon) for _, lat, lon in geometry]),
        )


if __name__ == "__main__":
    unittest.main()
