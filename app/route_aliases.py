"""Resolution between canonical routeids and the SubRouteUIDs behind them.

Some authorities — 公路客運 (THB) and most counties — publish each direction of a
route as its own ``SubRouteUID``. ``sync_static`` merges those direction siblings
into a single route with two paths, recording the original ids in the
``route_subroutes`` table. Two directions of traffic need that table:

* **outbound** (``subroute_uids``) — TDX realtime is filtered by ``SubRouteUID``,
  so a merged route must be queried by every id it absorbed, or the return
  direction never reports arrivals.
* **inbound** (``canonical``) — TDX responses and stale client routeids both
  arrive as the original SubRouteUID and must collapse back onto the surviving
  route.

The index is process-cached and invalidated by the database file's stat
fingerprint: ``sync_static`` swaps the file in with ``temp.replace(main)``, so a
new inode/mtime is a reliable signal that the mapping changed.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.config import Settings
from app.db import (
    _stat_fingerprint,
    get_readonly_connection,
    load_route_subroute_expansion,
    load_route_subroute_map,
)

LOGGER = logging.getLogger(__name__)

# How long a loaded index may be trusted before the file is stat()ed again. This
# keeps the syscall off the per-request path without making a post-sync swap take
# noticeably long to be picked up.
_FRESHNESS_TTL_SECONDS = 1.0


class RouteAliasIndex:
    """Immutable snapshot of the ``route_subroutes`` table."""

    __slots__ = ("_canonical", "_expansion")

    def __init__(
        self,
        canonical: dict[str, str],
        expansion: dict[str, list[str]],
    ) -> None:
        self._canonical = canonical
        self._expansion = expansion

    def canonical(self, routeid: str) -> str:
        """Collapse a SubRouteUID onto the routeid that absorbed it."""
        return self._canonical.get(routeid, routeid)

    def subroute_uids(self, routeid: str) -> list[str]:
        """Every SubRouteUID a route must be queried by, ordered by direction."""
        members = self._expansion.get(routeid)
        if members:
            return list(members)
        return [routeid]

    def is_alias(self, routeid: str) -> bool:
        """True when ``routeid`` was absorbed into a different canonical route."""
        mapped = self._canonical.get(routeid)
        return mapped is not None and mapped != routeid

    def __len__(self) -> int:
        return len(self._canonical)


_EMPTY_INDEX = RouteAliasIndex({}, {})

_cache_lock = threading.RLock()
_cached_index: RouteAliasIndex = _EMPTY_INDEX
_cached_path: Path | None = None
_cached_fingerprint: tuple[int, int, int] | None = None
_cached_checked_at: float = 0.0


def load_route_alias_index(db_path: str | Path) -> RouteAliasIndex:
    """Read the alias table directly, bypassing the cache."""
    try:
        with get_readonly_connection(db_path) as connection:
            canonical = load_route_subroute_map(connection)
            expansion = load_route_subroute_expansion(connection)
    except Exception:
        LOGGER.warning("failed to load route alias index from %s", db_path, exc_info=True)
        return _EMPTY_INDEX

    if not canonical:
        return _EMPTY_INDEX
    return RouteAliasIndex(canonical, expansion)


def get_route_alias_index(settings: Settings) -> RouteAliasIndex:
    """Return the cached alias index, reloading it when the database changed."""
    global _cached_index, _cached_path, _cached_fingerprint, _cached_checked_at

    raw_path = getattr(settings, "db_path", None)
    if raw_path is None:
        return _EMPTY_INDEX

    db_path = Path(raw_path)
    now = time.monotonic()

    with _cache_lock:
        same_path = _cached_path == db_path
        if same_path and now - _cached_checked_at < _FRESHNESS_TTL_SECONDS:
            return _cached_index

        fingerprint = _stat_fingerprint(db_path)
        if same_path and fingerprint == _cached_fingerprint:
            _cached_checked_at = now
            return _cached_index

        index = load_route_alias_index(db_path)
        _cached_index = index
        _cached_path = db_path
        _cached_fingerprint = fingerprint
        _cached_checked_at = now
        LOGGER.info("route alias index loaded entries=%s db=%s", len(index), db_path)
        return index


def reset_route_alias_cache() -> None:
    """Drop the cached index. Used by tests and after an in-process sync."""
    global _cached_index, _cached_path, _cached_fingerprint, _cached_checked_at

    with _cache_lock:
        _cached_index = _EMPTY_INDEX
        _cached_path = None
        _cached_fingerprint = None
        _cached_checked_at = 0.0
