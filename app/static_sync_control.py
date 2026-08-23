"""On-demand triggering of the static sync, shared with the weekly scheduler.

``sync_static`` builds ``bus.db.tmp`` and atomically swaps it over ``bus.db``, so
two concurrent runs would fight over the same temp file. Rather than spawn a
thread per request, the admin endpoint hands the work to the scheduler thread
that already exists: it waits on a trigger event as well as the clock, so a
manual run and the Monday 04:00 run can never overlap.

Deployments where the operator can only start and stop the process have no other
way to sync — the CLI needs shell access and a restart alone does not sync.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from app.logging_utils import get_logger

LOGGER = get_logger("static_sync")

# Terminal states are kept so the operator can poll for the outcome.
STATE_IDLE = "idle"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"


class StaticSyncCoordinator:
    """Single-slot work queue for the static sync."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trigger = threading.Event()
        self._state = STATE_IDLE
        self._cities: tuple[str, ...] | None = None
        self._force = False
        self._source: str | None = None
        self._requested_at: float | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._last_source: str | None = None
        self._last_error: str | None = None
        self._last_duration: float | None = None

    # -- producer side (the admin endpoint) --------------------------------

    def request(
        self,
        *,
        cities: tuple[str, ...] | None = None,
        force: bool = False,
        source: str = "manual",
    ) -> bool:
        """Queue a run. Returns False when one is already queued or running."""
        with self._lock:
            if self._state != STATE_IDLE:
                return False
            self._state = STATE_QUEUED
            self._cities = cities
            self._force = force
            self._source = source
            self._requested_at = time.time()
            self._started_at = None
            self._finished_at = None
            self._last_error = None
        self._trigger.set()
        LOGGER.info(
            "static sync requested source=%s cities=%s force=%s",
            source,
            ",".join(cities) if cities else "all",
            force,
        )
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "source": self._source if self._state != STATE_IDLE else self._last_source,
                "cities": list(self._cities) if self._cities else None,
                "force": self._force,
                "requested_at": _as_int(self._requested_at),
                "started_at": _as_int(self._started_at),
                "finished_at": _as_int(self._finished_at),
                "duration_seconds": (
                    round(self._last_duration, 1) if self._last_duration is not None else None
                ),
                "last_error": self._last_error,
            }

    # -- consumer side (the scheduler thread) ------------------------------

    def wait_for_trigger(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds. True when a manual run was queued."""
        return self._trigger.wait(timeout=timeout)

    def claim(self) -> tuple[tuple[str, ...] | None, bool]:
        """Take ownership of a queued run and mark it running."""
        self._trigger.clear()
        with self._lock:
            self._state = STATE_RUNNING
            self._started_at = time.time()
            return self._cities, self._force

    def begin_scheduled(self) -> None:
        with self._lock:
            self._state = STATE_RUNNING
            self._source = "schedule"
            self._cities = None
            self._force = False
            self._requested_at = time.time()
            self._started_at = self._requested_at
            self._last_error = None

    def finish(self, error: BaseException | None = None) -> None:
        with self._lock:
            self._finished_at = time.time()
            if self._started_at is not None:
                self._last_duration = self._finished_at - self._started_at
            self._last_source = self._source
            self._last_error = None if error is None else f"{type(error).__name__}: {error}"
            self._state = STATE_IDLE
            self._cities = None
            self._force = False
            self._source = None

    def run_claimed(self, runner: Callable[..., Any]) -> None:
        """Run ``runner`` for a claimed request, recording the outcome."""
        cities, force = self.claim()
        try:
            LOGGER.info(
                "manual static sync started cities=%s force=%s",
                ",".join(cities) if cities else "all",
                force,
            )
            runner(cities=cities, force=force)
            LOGGER.info("manual static sync completed")
            self.finish()
        except Exception as exc:
            LOGGER.exception("manual static sync failed: %s", exc)
            self.finish(exc)


def _as_int(value: float | None) -> int | None:
    return None if value is None else int(value)
