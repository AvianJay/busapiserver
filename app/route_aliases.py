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
    load_route_uid_rows,
)

LOGGER = logging.getLogger(__name__)

# Authorities whose realtime feeds stopped populating SubRouteUID (items carry
# only RouteUID + Direction) and whose local routeids follow the
# "RouteUID + trailing digit" convention. Databases that predate the
# route_uids table get a naming-convention-derived mapping for these prefixes
# so realtime recovers on deploy without waiting for a static re-sync.
_ROUTE_UID_DERIVATION_PREFIXES = ("TPE", "NWT")

# How long a loaded index may be trusted before the file is stat()ed again. This
# keeps the syscall off the per-request path without making a post-sync swap take
# noticeably long to be picked up.
_FRESHNESS_TTL_SECONDS = 1.0


class RouteAliasIndex:
    """Immutable snapshot of the ``route_subroutes`` and ``route_uids`` tables."""

    __slots__ = (
        "_canonical",
        "_expansion",
        "_routeid_by_uid",
        "_routeid_by_uid_dir",
        "_uid_by_routeid",
    )

    def __init__(
        self,
        canonical: dict[str, str],
        expansion: dict[str, list[str]],
        routeid_by_uid: dict[str, str] | None = None,
        routeid_by_uid_dir: dict[tuple[str, int], str] | None = None,
        uid_by_routeid: dict[str, str] | None = None,
    ) -> None:
        self._canonical = canonical
        self._expansion = expansion
        self._routeid_by_uid = routeid_by_uid or {}
        self._routeid_by_uid_dir = routeid_by_uid_dir or {}
        self._uid_by_routeid = uid_by_routeid or {}

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

    def routeid_for_route_uid(
        self,
        route_uid: str,
        direction: int | None = None,
    ) -> str | None:
        """The local routeid serving ``route_uid``, or None when unknown.

        Used to resolve realtime items whose SubRouteUID is null (雙北). A
        RouteUID shared by several distinct routes is ambiguous and resolves to
        None — feeds that only carry RouteUID fundamentally cannot say which
        區間/lettered variant an item belongs to, so failing closed beats
        mixing variants together.
        """
        if direction is not None:
            mapped = self._routeid_by_uid_dir.get((route_uid, direction))
            if mapped is not None:
                return mapped
        return self._routeid_by_uid.get(route_uid)

    def route_uid_for(self, routeid: str) -> str | None:
        """The TDX RouteUID to query ``routeid`` by, or None when unknown."""
        return self._uid_by_routeid.get(routeid)

    def __len__(self) -> int:
        return len(self._canonical)


_EMPTY_INDEX = RouteAliasIndex({}, {})


def _build_route_uid_maps(
    rows: list[tuple[str, int, str]],
) -> tuple[dict[str, str], dict[tuple[str, int], str], dict[str, str]]:
    """Collapse route_uids rows into lookup maps, dropping ambiguous keys."""
    routeids_by_uid: dict[str, set[str]] = {}
    routeids_by_uid_dir: dict[tuple[str, int], set[str]] = {}
    uids_by_routeid: dict[str, set[str]] = {}
    for route_uid, direction, routeid in rows:
        routeids_by_uid.setdefault(route_uid, set()).add(routeid)
        routeids_by_uid_dir.setdefault((route_uid, direction), set()).add(routeid)
        uids_by_routeid.setdefault(routeid, set()).add(route_uid)

    routeid_by_uid = {
        uid: next(iter(routeids))
        for uid, routeids in routeids_by_uid.items()
        if len(routeids) == 1
    }
    routeid_by_uid_dir = {
        key: next(iter(routeids))
        for key, routeids in routeids_by_uid_dir.items()
        if len(routeids) == 1
    }
    uid_by_routeid = {
        routeid: next(iter(uids))
        for routeid, uids in uids_by_routeid.items()
        if len(uids) == 1
    }

    ambiguous = len(routeids_by_uid) - len(routeid_by_uid)
    if ambiguous:
        LOGGER.info(
            "route uid index: %s RouteUIDs shared by multiple routes left unmapped",
            ambiguous,
        )
    return routeid_by_uid, routeid_by_uid_dir, uid_by_routeid


def _derive_legacy_route_uid_maps(
    connection,
    uid_rows: list[tuple[str, int, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """RouteUID maps derived from naming conventions, for pre-route_uids DBs.

    雙北 local routeids follow "RouteUID + trailing digit" (TPE101320 belongs
    to RouteUID TPE10132), and the RouteUID-keyed "stub" rows their static
    shape feed leaves behind (no stops, name == routeid) prove which trimmed
    ids are genuine RouteUIDs. A candidate key is only kept when it points at
    exactly one real (stop-carrying) route, so 區間 variant families and
    identity/variant shadow pairs fail closed instead of guessing.
    """
    covered_prefixes = {routeid[:3] for _uid, _direction, routeid in uid_rows}
    routeid_by_uid: dict[str, str] = {}
    uid_by_routeid: dict[str, str] = {}

    for prefix in _ROUTE_UID_DERIVATION_PREFIXES:
        if prefix in covered_prefixes:
            continue

        pattern = f"{prefix}%"
        all_routeids = {
            row["routeid"]
            for row in connection.execute(
                "SELECT routeid FROM routes WHERE routeid LIKE ?", (pattern,)
            )
        }
        if not all_routeids:
            continue
        real_routeids = {
            row["routeid"]
            for row in connection.execute(
                "SELECT DISTINCT routeid FROM stops WHERE routeid LIKE ?",
                (pattern,),
            )
        } & all_routeids
        stub_routeids = all_routeids - real_routeids

        candidates: dict[str, set[str]] = {}
        for routeid in real_routeids:
            candidates.setdefault(routeid, set()).add(routeid)
            if len(routeid) > 4 and routeid[-1].isdigit():
                candidates.setdefault(routeid[:-1], set()).add(routeid)
        for uid, routeids in candidates.items():
            if len(routeids) == 1:
                routeid_by_uid[uid] = next(iter(routeids))

        # Outbound side: only trim when the stub proves TDX really uses the
        # trimmed id as this route's RouteUID; anything else queries by its own
        # id, which is exact for identity-style routes and merely a miss (no
        # data until the next static sync) for unproven ones.
        for routeid in real_routeids:
            trimmed = (
                routeid[:-1]
                if len(routeid) > 4 and routeid[-1].isdigit()
                else ""
            )
            uid_by_routeid[routeid] = trimmed if trimmed in stub_routeids else routeid

        LOGGER.info(
            "route uid index: derived %s legacy mappings for prefix %s",
            sum(1 for routeid in uid_by_routeid if routeid.startswith(prefix)),
            prefix,
        )

    return routeid_by_uid, uid_by_routeid

_cache_lock = threading.RLock()
_cached_index: RouteAliasIndex = _EMPTY_INDEX
_cached_path: Path | None = None
_cached_fingerprint: tuple[int, int, int] | None = None
_cached_checked_at: float = 0.0


def load_route_alias_index(db_path: str | Path) -> RouteAliasIndex:
    """Read the alias tables directly, bypassing the cache."""
    try:
        with get_readonly_connection(db_path) as connection:
            canonical = load_route_subroute_map(connection)
            expansion = load_route_subroute_expansion(connection)
            uid_rows = load_route_uid_rows(connection)
            routeid_by_uid, routeid_by_uid_dir, uid_by_routeid = (
                _build_route_uid_maps(uid_rows)
            )
            derived_by_uid, derived_uid_by_routeid = _derive_legacy_route_uid_maps(
                connection, uid_rows
            )
    except Exception:
        LOGGER.warning("failed to load route alias index from %s", db_path, exc_info=True)
        return _EMPTY_INDEX

    # Persisted rows win over naming-convention derivation.
    for uid, routeid in derived_by_uid.items():
        routeid_by_uid.setdefault(uid, routeid)
    for routeid, uid in derived_uid_by_routeid.items():
        uid_by_routeid.setdefault(routeid, uid)

    if not canonical and not routeid_by_uid and not uid_by_routeid:
        return _EMPTY_INDEX
    return RouteAliasIndex(
        canonical,
        expansion,
        routeid_by_uid,
        routeid_by_uid_dir,
        uid_by_routeid,
    )


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
