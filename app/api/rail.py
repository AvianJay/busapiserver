from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Query, Request

from app.logging_utils import get_logger

LOGGER = get_logger("rail")

router = APIRouter(prefix="/api/v1", tags=["rail"])

# ── Caching ──────────────────────────────────────────────────────────────────

STATIC_CACHE_TTL = 3600
REALTIME_CACHE_TTL = 30
TIMETABLE_CACHE_TTL = 300  # 5 min for daily timetables


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


# ══════════════════════════════════════════════════════════════════════════════
#  THSR (Taiwan High Speed Rail)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/thsr/stations")
async def thsr_stations(request: Request):
    """Get all THSR stations."""
    cache_key = "thsr_stations"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/Station")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_class": item.get("StationClass", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/thsr/timetable/od")
async def thsr_timetable_od(
    request: Request,
    origin: str = Query(..., description="Origin station ID"),
    dest: str = Query(..., description="Destination station ID"),
    date: str = Query("", description="Date in YYYY-MM-DD, empty for today"),
):
    """Query THSR OD timetable."""
    cache_key = f"thsr_od_{origin}_{dest}_{date}"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    if date:
        path = f"/v2/Rail/THSR/DailyTimetable/OD/{origin}/to/{dest}/{date}"
    else:
        path = f"/v2/Rail/THSR/DailyTimetable/OD/{origin}/to/{dest}/today"
    raw = tdx.fetch_paginated_items(path)

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        origin_stop = item.get("OriginStopTime") or {}
        dest_stop = item.get("DestinationStopTime") or {}
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "origin_departure": origin_stop.get("DepartureTime", ""),
            "dest_arrival": dest_stop.get("ArrivalTime", ""),
            "origin_station_id": origin_stop.get("StationID", ""),
            "dest_station_id": dest_stop.get("StationID", ""),
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/thsr/timetable/today")
async def thsr_timetable_today(request: Request):
    """Get all THSR trains today."""
    cache_key = "thsr_today"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/DailyTimetable/Today")

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        stop_times = []
        for st in item.get("StopTimes") or []:
            stop_times.append({
                "station_id": st.get("StationID", ""),
                "station_name": (st.get("StationName") or {}).get("Zh_tw", ""),
                "arrival": st.get("ArrivalTime", ""),
                "departure": st.get("DepartureTime", ""),
            })
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "stop_times": stop_times,
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/thsr/seats/{station_id}")
async def thsr_seats(station_id: str, request: Request):
    """Get real-time seat availability at a THSR station."""
    cache_key = f"thsr_seats_{station_id}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/THSR/AvailableSeatStatusList/{station_id}",
    )

    result = []
    for item in raw:
        for train in item.get("AvailableSeats") or []:
            cars = []
            for car in train.get("StopStations") or []:
                cars.append({
                    "station_id": car.get("StationID", ""),
                    "station_name": (car.get("StationName") or {}).get("Zh_tw", ""),
                    "standard_seat": car.get("StandardSeatStatus", ""),
                    "business_seat": car.get("BusinessSeatStatus", ""),
                })
            result.append({
                "train_no": train.get("TrainNo", ""),
                "direction": train.get("Direction", 0),
                "departure_time": train.get("DepartureTime", ""),
                "destination": (train.get("EndingStationName") or {}).get("Zh_tw", ""),
                "seat_info": cars,
            })

    _set_cached(cache_key, result)
    return result


@router.get("/thsr/alerts")
async def thsr_alerts(request: Request):
    """Get THSR operational alerts."""
    cache_key = "thsr_alerts"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/AlertInfo")

    alerts = _parse_rail_alerts(raw)
    _set_cached(cache_key, alerts)
    return alerts


@router.get("/thsr/shape")
async def thsr_shape(request: Request):
    """Get THSR route geometry."""
    cache_key = "thsr_shape"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/Shape")

    shapes = []
    for item in raw:
        shapes.append({
            "line_name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "geometry": item.get("Geometry", ""),
        })

    _set_cached(cache_key, shapes)
    return shapes


# ══════════════════════════════════════════════════════════════════════════════
#  TRA (Taiwan Railway Administration)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tra/stations")
async def tra_stations(request: Request):
    """Get all TRA stations."""
    cache_key = "tra_stations"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Station")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_class": item.get("StationClass", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/tra/lines")
async def tra_lines(request: Request):
    """Get all TRA lines."""
    cache_key = "tra_lines"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Line")

    lines = []
    for item in raw:
        lines.append({
            "line_id": item.get("LineID", ""),
            "line_no": item.get("LineNo", ""),
            "name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("LineName") or {}).get("En", ""),
        })

    _set_cached(cache_key, lines)
    return lines


@router.get("/tra/timetable/od")
async def tra_timetable_od(
    request: Request,
    origin: str = Query(..., description="Origin station ID"),
    dest: str = Query(..., description="Destination station ID"),
    date: str = Query("", description="Date YYYY-MM-DD, empty for today"),
):
    """Query TRA OD timetable."""
    cache_key = f"tra_od_{origin}_{dest}_{date}"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    if date:
        path = f"/v2/Rail/TRA/DailyTimetable/OD/{origin}/to/{dest}/{date}"
    else:
        path = f"/v2/Rail/TRA/DailyTimetable/OD/{origin}/to/{dest}/today"
    raw = tdx.fetch_paginated_items(path)

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        origin_stop = item.get("OriginStopTime") or {}
        dest_stop = item.get("DestinationStopTime") or {}
        train_type = (train_info.get("TrainTypeName") or {}).get("Zh_tw", "")
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "train_type": train_type,
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "origin_departure": origin_stop.get("DepartureTime", ""),
            "dest_arrival": dest_stop.get("ArrivalTime", ""),
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/tra/liveboard/{station_id}")
async def tra_liveboard(station_id: str, request: Request):
    """Get real-time arrival/departure at a TRA station."""
    cache_key = f"tra_live_{station_id}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/TRA/LiveBoard/Station/{station_id}",
    )

    entries = []
    for item in raw:
        train_type = (item.get("TrainTypeName") or {}).get("Zh_tw", "")
        entries.append({
            "train_no": item.get("TrainNo", ""),
            "train_type": train_type,
            "station_id": item.get("StationID", ""),
            "station_name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "end_station": (item.get("EndingStationName") or {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "scheduled_arrival": item.get("ScheduledArrivalTime", ""),
            "scheduled_departure": item.get("ScheduledDepartureTime", ""),
            "delay_minutes": item.get("DelayTime", 0),
        })

    _set_cached(cache_key, entries)
    return entries


@router.get("/tra/shape")
async def tra_shape(request: Request):
    """Get TRA route geometry."""
    cache_key = "tra_shape"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Shape")

    shapes = []
    for item in raw:
        shapes.append({
            "line_id": item.get("LineID", ""),
            "line_name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "geometry": item.get("Geometry", ""),
        })

    _set_cached(cache_key, shapes)
    return shapes


@router.get("/tra/alerts")
async def tra_alerts(request: Request):
    """Get TRA operational alerts."""
    cache_key = "tra_alerts"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/AlertInfo")

    alerts = _parse_rail_alerts(raw)
    _set_cached(cache_key, alerts)
    return alerts


# ══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_rail_alerts(raw: list[dict]) -> list[dict]:
    alerts = []
    for item in raw:
        # Title / Description may be plain strings or NameType dicts.
        title_raw = item.get("Title") or ""
        if isinstance(title_raw, dict):
            title_raw = title_raw.get("Zh_tw") or title_raw.get("En") or ""
        desc_raw = item.get("Description") or ""
        if isinstance(desc_raw, dict):
            desc_raw = desc_raw.get("Zh_tw") or desc_raw.get("En") or ""
        alerts.append({
            "alert_id": item.get("AlertID", ""),
            "title": title_raw,
            "description": desc_raw,
            "status": item.get("Status", 0),
            "scope": item.get("Scope", ""),
            "direction": item.get("Direction"),
            "publish_time": item.get("PublishTime", ""),
            "update_time": item.get("UpdateTime", ""),
            "start_time": item.get("StartTime", ""),
            "end_time": item.get("EndTime", ""),
        })
    return alerts
