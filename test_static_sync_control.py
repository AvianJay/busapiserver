"""Tests for admin-triggered static syncs.

The load-bearing property is that a manual run can never overlap the scheduled
one: sync_static swaps bus.db.tmp over bus.db, so two concurrent runs would
corrupt each other.
"""

from __future__ import annotations

import threading
import unittest

from app.static_sync_control import (
    STATE_IDLE,
    STATE_QUEUED,
    STATE_RUNNING,
    StaticSyncCoordinator,
)


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = StaticSyncCoordinator()

    def test_starts_idle(self) -> None:
        status = self.coordinator.status()
        self.assertEqual(status["state"], STATE_IDLE)
        self.assertIsNone(status["started_at"])
        self.assertIsNone(status["last_error"])

    def test_request_queues_and_wakes_the_scheduler(self) -> None:
        self.assertTrue(self.coordinator.request(cities=("Hsinchu",), source="admin"))

        status = self.coordinator.status()
        self.assertEqual(status["state"], STATE_QUEUED)
        self.assertEqual(status["cities"], ["Hsinchu"])
        self.assertEqual(status["source"], "admin")
        # The scheduler thread is sitting in wait_for_trigger.
        self.assertTrue(self.coordinator.wait_for_trigger(timeout=0))

    def test_second_request_is_rejected_while_queued(self) -> None:
        self.assertTrue(self.coordinator.request())
        self.assertFalse(self.coordinator.request())

    def test_second_request_is_rejected_while_running(self) -> None:
        self.coordinator.request()
        self.coordinator.claim()
        self.assertEqual(self.coordinator.status()["state"], STATE_RUNNING)
        self.assertFalse(self.coordinator.request())

    def test_request_is_accepted_again_once_finished(self) -> None:
        self.coordinator.request()
        self.coordinator.claim()
        self.coordinator.finish()
        self.assertEqual(self.coordinator.status()["state"], STATE_IDLE)
        self.assertTrue(self.coordinator.request())

    def test_run_claimed_passes_through_cities_and_force(self) -> None:
        seen: dict[str, object] = {}

        def runner(*, cities, force):
            seen["cities"] = cities
            seen["force"] = force
            seen["state"] = self.coordinator.status()["state"]

        self.coordinator.request(cities=("Hsinchu", "Taipei"), force=True)
        self.coordinator.run_claimed(runner)

        self.assertEqual(seen["cities"], ("Hsinchu", "Taipei"))
        self.assertTrue(seen["force"])
        self.assertEqual(seen["state"], STATE_RUNNING)
        status = self.coordinator.status()
        self.assertEqual(status["state"], STATE_IDLE)
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["finished_at"])

    def test_failure_is_recorded_and_does_not_wedge_the_coordinator(self) -> None:
        def runner(*, cities, force):
            raise RuntimeError("TDX exploded")

        self.coordinator.request()
        self.coordinator.run_claimed(runner)

        status = self.coordinator.status()
        self.assertEqual(status["state"], STATE_IDLE)
        self.assertEqual(status["last_error"], "RuntimeError: TDX exploded")
        # A failed run must not block the next attempt.
        self.assertTrue(self.coordinator.request())

    def test_trigger_is_cleared_so_the_scheduler_does_not_loop(self) -> None:
        self.coordinator.request()
        self.coordinator.run_claimed(lambda *, cities, force: None)
        self.assertFalse(self.coordinator.wait_for_trigger(timeout=0))

    def test_scheduled_run_blocks_a_concurrent_manual_request(self) -> None:
        self.coordinator.begin_scheduled()
        self.assertEqual(self.coordinator.status()["state"], STATE_RUNNING)
        self.assertFalse(self.coordinator.request())
        self.coordinator.finish()
        self.assertTrue(self.coordinator.request())

    def test_only_one_of_many_concurrent_requests_wins(self) -> None:
        accepted: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            result = self.coordinator.request()
            with lock:
                accepted.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(accepted), 1)


if __name__ == "__main__":
    unittest.main()
