"""City-wide live bus positions for the 全公車地圖 map screen.

This router deliberately does NOT carry ``Depends(enforce_rate_limit)``. A map
polls every ~15 s, which would eat a fifth of the shared 60/min budget that the
rest of the app needs for ETA and favourites. It spends its own bucket instead,
enforced below. Moving these routes into ``app.api.routes`` would silently
re-apply the global limit on top, so keep them here.
"""

from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException, Request, Response

from app.api.routes import _resolve_city
from app.config import INTERCITY_CITY_NAME
from app.logging_utils import get_logger
from app.rate_limit import check_rate_limit, request_rate_limit_key

LOGGER = get_logger("city_buses_api")

router = APIRouter(tags=["Bus"])

_RATE_LIMIT_DETAIL = "全公車地圖更新過於頻繁，請稍後再試。"


@router.get("/api/v1/cities/{city}/buses")
def get_city_buses(city: str, request: Request, response: Response) -> dict:
    resolved = _resolve_city(city)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"City {city} was not found.")
    prefix, city_name = resolved

    settings = request.app.state.settings
    if city_name != INTERCITY_CITY_NAME and city_name not in settings.tdx_cities:
        raise HTTPException(status_code=404, detail=f"City {city} is not served.")

    check_rate_limit(
        request_rate_limit_key(request),
        "city-buses",
        requests=settings.city_buses_rate_limit_requests,
        window_seconds=60,
        detail=_RATE_LIMIT_DETAIL,
    )

    service = request.app.state.city_buses_service
    try:
        payload = service.get_city_buses(city_name, prefix)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response.headers["Cache-Control"] = f"private, max-age={settings.city_buses_cache_ttl}"
    if payload.get("stale"):
        response.headers["X-City-Buses-Stale"] = "1"
    return payload
