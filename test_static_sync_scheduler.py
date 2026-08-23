"""Integration tests for the scheduler thread that services manual syncs."""

from __future__ import annotations

from datetime import datetime
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from app.main import _next_monday_4am, _run_weekly_static_sync
from app.static_sync_control import StaticSyncCoordinator


class NextMondayTests(unittest.TestCase):
    def test_sunday_evening_schedules_the_next_morning(self) -> None:
        target = _next_monday_4am(datetime(2026, 8, 23, 18, 30))
        self.assertEqual(target, datetime(2026, 8, 24, 4, 0))

    def test_monday_after_four_schedules_a_week_out(self) -> None:
        target = _next_monday_4am(datetime(2026, 8, 24, 5, 0))
        self.assertEqual(target, datetime(2026, 8, 31, 4, 0))


class SchedulerThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = StaticSyncCoordinator()
        self.stop_event = threading.Event()
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(db_path="unused"),
                static_sync_coordinator=self.coordinator,
            )
        )
        self.sync_calls: list[dict] = []
        self.sync_started = threading.Event()

        def fake_sync(settings, *, cities=None, force=False):
            self.sync_calls.append({"cities": cities, "force": force})
            self.sync_started.set()

        self.patches = [
            mock.patch("app.main.sync_static", side_effect=fake_sync),
            mock.patch("app.main._reconcile_database_versions"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        self.stop_event.set()
        for patcher in self.patches:
            patcher.stop()

    def _start_thread(self) -> threading.Thread:
        thread = threading.Thread(
            target=_run_weekly_static_sync,
            args=(self.app, self.stop_event),
            daemon=True,
        )
        thread.start()
        return thread

    def test_admin_request_wakes_the_thread_and_runs_the_sync(self) -> None:
        thread = self._start_thread()
        try:
            self.coordinator.request(cities=("Hsinchu",), source="admin")
            self.assertTrue(
                self.sync_started.wait(timeout=10),
                "scheduler thread did not pick up the queued sync",
            )
        finally:
            self.stop_event.set()
            thread.join(timeout=10)

        self.assertEqual(self.sync_calls, [{"cities": ("Hsinchu",), "force": False}])
        self.assertEqual(self.coordinator.status()["state"], "idle")

    def test_thread_shuts_down_promptly_while_waiting(self) -> None:
        thread = self._start_thread()
        # Give it a moment to settle into the wait loop.
        self.assertFalse(self.sync_started.wait(timeout=0.5))

        started = time.monotonic()
        self.stop_event.set()
        thread.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertFalse(thread.is_alive())
        # The scheduler ticks once a second; anything near the old 60s chunk
        # would mean shutdown blocks on the trigger wait.
        self.assertLess(elapsed, 5.0)
        self.assertEqual(self.sync_calls, [])

    def test_thread_stays_alive_after_a_failing_manual_sync(self) -> None:
        thread = self._start_thread()
        try:
            with mock.patch("app.main.sync_static", side_effect=RuntimeError("boom")):
                self.coordinator.request(source="admin")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if self.coordinator.status()["last_error"]:
                        break
                    time.sleep(0.05)

            status = self.coordinator.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["last_error"], "RuntimeError: boom")
            self.assertTrue(thread.is_alive())

            # And a follow-up request is still serviced.
            self.coordinator.request(source="admin")
            self.assertTrue(self.sync_started.wait(timeout=10))
        finally:
            self.stop_event.set()
            thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
