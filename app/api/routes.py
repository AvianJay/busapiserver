from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.sync_realtime import RouteNotFoundError


router = APIRouter()


@router.get("/downloads/bus.db")
def download_bus_db(request: Request) -> FileResponse:
    db_path = request.app.state.settings.db_path
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="bus.db does not exist yet.")
    return FileResponse(db_path, filename="bus.db")


@router.get("/api/v1/routes/{routeid}/realtime")
def get_route_realtime(routeid: str, request: Request) -> dict:
    service = request.app.state.realtime_service
    try:
        return service.get_snapshot(routeid)
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
