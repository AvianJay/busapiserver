from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
import threading
import time

from fastapi import HTTPException, Request

from app.auth_service import AuthPrincipal, authenticate_token
from app.config import get_settings


RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
AUTH_TOKEN_COOKIE_NAME = "yabus_auth_token"

_rate_limit_lock = threading.Lock()
_rate_limit_hits: MutableMapping[tuple[str, str, int], deque[float]] = {}
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


def _extract_bearer_token(request: Request) -> str | None:
    authorization = (request.headers.get("authorization") or "").strip()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()

    cookie_token = (request.cookies.get(AUTH_TOKEN_COOKIE_NAME) or "").strip()
    if cookie_token:
        return cookie_token
    return None


def _authenticate_request(request: Request) -> AuthPrincipal | None:
    if hasattr(request.state, "auth_principal"):
        return request.state.auth_principal

    principal = None
    token = _extract_bearer_token(request)
    if token:
        settings = getattr(request.app.state, "settings", None) or get_settings()
        principal = authenticate_token(settings, token)
    request.state.auth_principal = principal
    return principal


def _prune_stale_hits(current: float) -> None:
    stale_keys = [
        key
        for key, hits in _rate_limit_hits.items()
        if not hits or hits[-1] < (current - key[2])
    ]
    for key in stale_keys:
        _rate_limit_hits.pop(key, None)


def check_rate_limit(
    client_ip: str,
    bucket: str,
    *,
    now: float | None = None,
    requests: int = RATE_LIMIT_REQUESTS,
    window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    detail: str = "Too many requests. Please try again later.",
) -> None:
    global _last_cleanup_at

    current = time.monotonic() if now is None else now
    window_start = current - window_seconds
    key = (client_ip, bucket, window_seconds)

    with _rate_limit_lock:
        if current - _last_cleanup_at >= RATE_LIMIT_WINDOW_SECONDS:
            _prune_stale_hits(current)
            _last_cleanup_at = current

        hits = _rate_limit_hits.setdefault(key, deque())
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= requests:
            retry_after = max(1, int(hits[0] + window_seconds - current))
            raise HTTPException(
                status_code=429,
                detail=detail,
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(current)


def enforce_rate_limit(request: Request) -> None:
    principal = _authenticate_request(request)
    if principal is None:
        bucket = f"ip:{_get_client_ip(request)}"
    else:
        bucket = f"user:{principal.account_id}"

    check_rate_limit(bucket, "global")


def get_request_principal(request: Request) -> AuthPrincipal | None:
    return _authenticate_request(request)


def reset_rate_limit_state() -> None:
    global _last_cleanup_at

    with _rate_limit_lock:
        _rate_limit_hits.clear()
        _last_cleanup_at = 0.0
