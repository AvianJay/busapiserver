from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db import init_db
from app.sync_realtime import RealtimeService
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.db_path)

    token_manager = TDXTokenManager(settings)
    tdx_client = TDXClient(settings, token_manager)
    realtime_service = RealtimeService(settings, tdx_client)

    app.state.settings = settings
    app.state.token_manager = token_manager
    app.state.tdx_client = tdx_client
    app.state.realtime_service = realtime_service

    try:
        yield
    finally:
        tdx_client.close()
        token_manager.close()


app = FastAPI(title="Bus API Server", lifespan=lifespan)
app.include_router(router)
