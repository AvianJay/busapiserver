from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.db import get_connection, load_path_points, path_exists
from app.sync_realtime import RouteNotFoundError


router = APIRouter()


def _encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    output: list[str] = []
    previous_lat = 0
    previous_lon = 0

    for lat, lon in points:
        lat_value = int(round(lat * factor))
        lon_value = int(round(lon * factor))
        for value in (lat_value - previous_lat, lon_value - previous_lon):
            encoded = value << 1
            if value < 0:
                encoded = ~encoded
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
        previous_lat = lat_value
        previous_lon = lon_value

    return "".join(output)


@router.get("/downloads/bus.db")
def download_bus_db(request: Request) -> FileResponse:
    db_path = request.app.state.settings.download_db_path
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


@router.get("/api/v1/routes/{routeid}/paths/{pathid}/points")
def get_route_path_points(routeid: str, pathid: int, request: Request) -> dict:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        if not path_exists(connection, routeid, pathid):
            raise HTTPException(
                status_code=404,
                detail=f"Path {pathid} for route {routeid} was not found.",
            )

        points = load_path_points(connection, routeid, pathid)

    return {
        "routeid": routeid,
        "pathid": pathid,
        "polyline": _encode_polyline(points),
    }
