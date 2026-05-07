from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
import threading
import time

from fastapi import HTTPException, Request


RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

_rate_limit_lock = threading.Lock()
_rate_limit_hits: MutableMapping[tuple[str, str], deque[float]] = {}
_last_cleanup_at = 0.0


def _get_client_ip(request: Request) -> str:
    # Trust Cloudflare's connecting IP first.
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    return (request.client.host if request.client else "unknown").strip() or "unknown"


def _resolve_route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    route_template = getattr(route, "path", request.url.path)
    if not isinstance(route_template, str) or not route_template:
        return None
    return route_template


def _normalize_bucket(route_template: str) -> str:
    # Keep all route realtime variants in the same bucket so they share the cap.
    if route_template.startswith("/api/v1/routes/") and "realtime" in route_template:
        return "/api/v1/routes/realtime"
    return route_template


def _prune_stale_hits(window_start: float) -> None:
    stale_keys = [
        key
        for key, hits in _rate_limit_hits.items()
        if not hits or hits[-1] < window_start
    ]
    for key in stale_keys:
        _rate_limit_hits.pop(key, None)


def check_rate_limit(client_ip: str, bucket: str, *, now: float | None = None) -> None:
    global _last_cleanup_at

    current = time.monotonic() if now is None else now
    window_start = current - RATE_LIMIT_WINDOW_SECONDS
    key = (client_ip, bucket)

    with _rate_limit_lock:
        if current - _last_cleanup_at >= RATE_LIMIT_WINDOW_SECONDS:
            _prune_stale_hits(window_start)
            _last_cleanup_at = current

        hits = _rate_limit_hits.setdefault(key, deque())
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, int(hits[0] + RATE_LIMIT_WINDOW_SECONDS - current))
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(current)


def enforce_rate_limit(request: Request) -> None:
    route_template = _resolve_route_template(request)
    if route_template is None:
        return

    check_rate_limit(_get_client_ip(request), _normalize_bucket(route_template))


def reset_rate_limit_state() -> None:
    global _last_cleanup_at

    with _rate_limit_lock:
        _rate_limit_hits.clear()
        _last_cleanup_at = 0.0
