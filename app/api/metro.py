from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request

from app.config import CITY_PREFIX_TO_NAME
from app.logging_utils import get_logger

LOGGER = get_logger("metro")

router = APIRouter(prefix="/api/v1/metro", tags=["metro"])

# Mapping of metro system codes to TDX city names.
METRO_SYSTEMS = {
    "TRTC": {"city": "Taipei", "name": "臺北捷運", "name_en": "Taipei Metro"},
    "KRTC": {"city": "Kaohsiung", "name": "高雄捷運", "name_en": "Kaohsiung Metro"},
    "TYMC": {"city": "Taoyuan", "name": "桃園捷運", "name_en": "Taoyuan Metro"},
    "TMRT": {"city": "Taichung", "name": "臺中捷運", "name_en": "Taichung Metro"},
}

# ── Caching ──────────────────────────────────────────────────────────────────

STATIC_CACHE_TTL = 3600  # 1 hour for lines/stations (rarely change)
LIVEBOARD_CACHE_TTL = 15  # 15 seconds for realtime


@dataclass
class _CacheEntry:
    data: object
    fetched_at: float = field(default_factory=time.monotonic)

    def is_fresh(self, ttl: float) -> bool:
        return (time.monotonic() - self.fetched_at) < ttl


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _get_cached(key: str, ttl: float) -> object | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry.is_fresh(ttl):
            return entry.data
    return None


def _set_cached(key: str, data: object) -> None:
    with _cache_lock:
        _cache[key] = _CacheEntry(data=data)


def _get_tdx(request: Request):
    return request.app.state.tdx_client


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/systems")
async def list_systems():
    """List supported metro systems."""
    return [
        {"system": code, **info}
        for code, info in METRO_SYSTEMS.items()
    ]


@router.get("/{system}/lines")
async def get_lines(system: str, request: Request):
    """Get all lines for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_lines_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Line/{system}")

    lines = []
    for item in raw:
        line_id = item.get("LineID", "")
        name_zh = (item.get("LineName") or {}).get("Zh_tw", "")
        name_en = (item.get("LineName") or {}).get("En", "")
        color = item.get("LineColor", "")
        line_no = item.get("LineNo", "")
        sections = []
        for sec in item.get("LineSectionList") or []:
            sections.append({
                "section_id": sec.get("LineSectionID", ""),
                "name": (sec.get("LineSectionName") or {}).get("Zh_tw", ""),
                "name_en": (sec.get("LineSectionName") or {}).get("En", ""),
            })
        lines.append({
            "line_id": line_id,
            "line_no": line_no,
            "name": name_zh,
            "name_en": name_en,
            "color": color,
            "sections": sections,
        })

    _set_cached(cache_key, lines)
    return lines


@router.get("/{system}/stations")
async def get_stations(system: str, request: Request):
    """Get all stations for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_stations_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Station/{system}")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_address": (item.get("StationAddress") or ""),
            "line_id": item.get("LineID", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/{system}/station-of-line")
async def get_station_of_line(system: str, request: Request):
    """Get station sequences per line for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_sol_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/StationOfLine/{system}")

    result = []
    for item in raw:
        line_id = item.get("LineID", "")
        direction = item.get("Direction", 0)
        stations_raw = item.get("Stations") or []
        stations = []
        for s in stations_raw:
            stations.append({
                "station_id": s.get("StationID", ""),
                "name": (s.get("StationName") or {}).get("Zh_tw", ""),
                "name_en": (s.get("StationName") or {}).get("En", ""),
                "sequence": s.get("Sequence", 0),
            })
        result.append({
            "line_id": line_id,
            "direction": direction,
            "stations": stations,
        })

    _set_cached(cache_key, result)
    return result


@router.get("/{system}/lines/{line_id}/liveboard")
async def get_liveboard(system: str, line_id: str, request: Request):
    """Get real-time arrival/departure info for a metro line."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_live_{system}_{line_id}"
    cached = _get_cached(cache_key, LIVEBOARD_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/Metro/LiveBoard/{system}",
        params={"$filter": f"LineID eq '{line_id}'", "$format": "JSON"},
    )

    entries = []
    for item in raw:
        entries.append({
            "station_id": item.get("StationID", ""),
            "station_name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "line_id": item.get("LineID", ""),
            "destination_id": item.get("DestinationStationID") or item.get("DestinationStaionID", ""),
            "destination_name": (item.get("DestinationStationName") or {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "trip_head_sign": item.get("TripHeadSign", ""),
            "train_no": item.get("TrainNo", ""),
            "estimated_time": item.get("EstimateTime"),  # seconds
            "service_status": item.get("ServiceStatus", 0),
        })

    _set_cached(cache_key, entries)
    return entries


@router.get("/{system}/lines/{line_id}/shape")
async def get_shape(system: str, line_id: str, request: Request):
    """Get route geometry for a metro line."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_shape_{system}_{line_id}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/Metro/Shape/{system}",
        params={"$filter": f"LineID eq '{line_id}'", "$format": "JSON"},
    )

    shapes = []
    for item in raw:
        geometry = item.get("Geometry", "")
        shapes.append({
            "line_id": item.get("LineID", ""),
            "direction": item.get("Direction", 0),
            "geometry": geometry,
        })

    _set_cached(cache_key, shapes)
    return shapes


@router.get("/{system}/frequency")
async def get_frequency(system: str, request: Request):
    """Get service frequency info for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_freq_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Frequency/{system}")

    result = []
    for item in raw:
        line_id = item.get("LineID", "")
        headways = []
        for hw in item.get("Headways") or []:
            headways.append({
                "peak_flag": hw.get("PeakFlag", ""),
                "start_time": hw.get("StartTime", ""),
                "end_time": hw.get("EndTime", ""),
                "min_headway": hw.get("MinHeadwayMins", 0),
                "max_headway": hw.get("MaxHeadwayMins", 0),
            })
        result.append({
            "line_id": line_id,
            "route_id": item.get("RouteID", ""),
            "service_day": item.get("ServiceDay", {}),
            "headways": headways,
        })

    _set_cached(cache_key, result)
    return result


@router.get("/{system}/s2s-traveltime")
async def get_s2s_traveltime(system: str, request: Request):
    """Get station-to-station travel time."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_s2s_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/S2STravelTime/{system}")

    result = []
    for item in raw:
        travel_times = []
        for tt in item.get("TravelTimes") or []:
            travel_times.append({
                "from_station_id": tt.get("FromStationID", ""),
                "from_station_name": (tt.get("FromStationName") or {}).get("Zh_tw", ""),
                "to_station_id": tt.get("ToStationID", ""),
                "to_station_name": (tt.get("ToStationName") or {}).get("Zh_tw", ""),
                "run_time": tt.get("RunTimeSecs", 0),
                "stop_time": tt.get("StopTimeSecs", 0),
            })
        result.append({
            "line_id": item.get("LineID", ""),
            "route_id": item.get("RouteID", ""),
            "direction": item.get("Direction", 0),
            "travel_times": travel_times,
        })

    _set_cached(cache_key, result)
    return result
