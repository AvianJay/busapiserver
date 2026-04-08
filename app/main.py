from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import threading

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db import init_db, refresh_database_versions
from app.sync_realtime import RealtimeService
from app.sync_static import sync_static
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient


def _next_monday_4am(now: datetime) -> datetime:
    scheduled = now.replace(hour=4, minute=0, second=0, microsecond=0)
    days_until_monday = (7 - now.weekday()) % 7
    if days_until_monday:
        scheduled = scheduled + timedelta(days=days_until_monday)
    if scheduled <= now:
        scheduled = scheduled + timedelta(days=7)
    return scheduled


def _run_weekly_static_sync(app: FastAPI, stop_event: threading.Event) -> None:
    settings = app.state.settings
    while not stop_event.is_set():
        now = datetime.now()
        target = _next_monday_4am(now)
        wait_seconds = max(0.0, (target - now).total_seconds())

        # Wait in chunks so shutdown can interrupt promptly.
        while wait_seconds > 0 and not stop_event.is_set():
            sleep_seconds = min(60.0, wait_seconds)
            stop_event.wait(timeout=sleep_seconds)
            wait_seconds -= sleep_seconds

        if stop_event.is_set():
            break

        try:
            print(f"[scheduler] weekly static sync started at {datetime.now().isoformat(timespec='seconds')}")
            sync_static(settings)
            print(f"[scheduler] weekly static sync completed at {datetime.now().isoformat(timespec='seconds')}")
        except Exception as exc:
            print(f"[scheduler] weekly static sync failed: {exc}")
            # Avoid tight error loop.
            if not stop_event.wait(timeout=60):
                continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)
    refresh_database_versions(
        settings.db_path,
        download_db_path=settings.download_db_path,
        city_db_paths={
            city: settings.city_db_path(city)
            for city in settings.tdx_cities
            if settings.city_db_path(city).exists()
        },
    )

    token_manager = TDXTokenManager(settings)
    tdx_client = TDXClient(settings, token_manager)
    realtime_service = RealtimeService(settings, tdx_client)
    scheduler_stop_event = threading.Event()
    scheduler_thread = threading.Thread(
        target=_run_weekly_static_sync,
        args=(app, scheduler_stop_event),
        daemon=True,
        name="weekly-static-sync",
    )

    app.state.settings = settings
    app.state.token_manager = token_manager
    app.state.tdx_client = tdx_client
    app.state.realtime_service = realtime_service
    app.state.scheduler_stop_event = scheduler_stop_event
    app.state.scheduler_thread = scheduler_thread

    scheduler_thread.start()

    try:
        yield
    finally:
        scheduler_stop_event.set()
        scheduler_thread.join(timeout=5)
        tdx_client.close()
        token_manager.close()


app = FastAPI(title="Bus API Server", lifespan=lifespan)
app.include_router(router)
