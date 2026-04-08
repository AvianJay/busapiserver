from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import routeid_to_city
from app.db import (
    get_connection,
    load_path_points,
    load_route_metadata,
    load_route_stops,
    path_exists,
)
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


def _load_route_static_for_api(settings, routeid: str) -> tuple[dict, dict[int, dict]]:
    with get_connection(settings.db_path) as main_connection:
        route_meta = load_route_metadata(main_connection, routeid)
    if route_meta is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")

    city = routeid_to_city(routeid)
    if city is None:
        raise HTTPException(status_code=404, detail=f"Could not resolve city for route {routeid}.")

    city_db_path = settings.city_db_path(city)
    if not city_db_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"City database for route {routeid} does not exist yet.",
        )

    with get_connection(city_db_path) as city_connection:
        stops_by_path = load_route_stops(city_connection, routeid)

    return route_meta, stops_by_path


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

    city = routeid_to_city(routeid)
    if city is None:
        raise HTTPException(status_code=404, detail=f"Could not resolve city for route {routeid}.")

    city_db_path = settings.city_db_path(city)
    if not city_db_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"City database for route {routeid} does not exist yet.",
        )

    with get_connection(city_db_path) as connection:
        points = load_path_points(connection, routeid, pathid)

    return {
        "routeid": routeid,
        "pathid": pathid,
        "polyline": _encode_polyline(points),
    }


@router.get("/api/v1/routes/{routeid}/stops")
def get_route_stops(routeid: str, request: Request) -> dict:
    settings = request.app.state.settings
    route_meta, stops_by_path = _load_route_static_for_api(settings, routeid)

    response_paths = []
    seen_pathids = set(route_meta["paths"])
    seen_pathids.update(stops_by_path)
    for pathid in sorted(seen_pathids):
        path_meta = route_meta["paths"].get(pathid, {})
        stops = list((stops_by_path.get(pathid) or {}).get("stops", []))
        response_paths.append(
            {
                "pathid": pathid,
                "name": path_meta.get("name") or f"Path {pathid}",
                "stops": stops,
            }
        )

    return {
        "routeid": routeid,
        "name": route_meta["name"],
        "paths": response_paths,
    }
