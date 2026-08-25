"""One-time rewrite of saved favorites whose routeid was absorbed by a merge.

Some authorities publish each direction of a route as its own SubRouteUID, and
``sync_static`` now merges those into a single route. Favorites that pointed at
the direction that lost the coin toss still resolve through the API's alias
layer, but the cloud document keeps the stale id forever — and offline clients
that never reach the alias layer cannot resolve it at all.

Run this after the first merged static sync:

    python -m app.migrate_favorite_routeids --dry-run
    python -m app.migrate_favorite_routeids

Only ``favorites`` documents are touched. ``pathId`` and ``stopId`` are left
alone: the merge preserves ``pathid == Direction``, so a favorite's path still
points at the same physical direction of travel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from typing import Any

from app.config import Settings, get_settings
from app.db import get_connection
from app.logging_utils import get_logger, setup_logging, shutdown_logging
from app.route_aliases import load_route_alias_index

LOGGER = get_logger("migrate_favorite_routeids")

_FNV_OFFSET_BASIS = 0x811C9DC5
_FNV_PRIME = 0x01000193


def route_key_for_routeid(routeid: str) -> int:
    """Reproduce the client's route key hash bit for bit.

    Mirrors ``_routeKeyForRouteId`` in lib/core/bus_repository.dart. Note the
    mask is applied after *each* multiply rather than once at the end, and the
    input is iterated as UTF-16 code units — both are load-bearing, because a
    key computed any other way will not match what the app looks up.
    """
    hash_value = _FNV_OFFSET_BASIS
    for code_unit in _utf16_code_units(routeid):
        hash_value ^= code_unit
        hash_value = (hash_value * _FNV_PRIME) & 0x7FFFFFFF
    return hash_value


def _utf16_code_units(value: str) -> list[int]:
    raw = value.encode("utf-16-le")
    return [raw[index] | (raw[index + 1] << 8) for index in range(0, len(raw), 2)]


def _dump_payload_json(payload: dict[str, Any]) -> str:
    # Must match app.account_sync_service._dump_payload_json so that the stored
    # content_hash stays comparable with what the sync endpoints produce.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _rewrite_favorites_payload(
    payload: dict[str, Any],
    canonical_for: dict[str, str],
) -> tuple[dict[str, Any], int, int]:
    """Return (payload, rewritten_count, dropped_duplicate_count)."""
    groups = payload.get("groups")
    if not isinstance(groups, dict):
        return payload, 0, 0

    rewritten = 0
    dropped = 0
    new_groups: dict[str, list[dict[str, Any]]] = {}

    for group_name, items in groups.items():
        if not isinstance(items, list):
            new_groups[group_name] = items
            continue

        new_items: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any, Any]] = set()
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue

            item_type = item.get("type", "boarding")
            routeid = item.get("routeId") if item_type in {"route", "boarding"} else None
            canonical = canonical_for.get(routeid) if isinstance(routeid, str) else None
            if canonical is not None and canonical != routeid:
                item = dict(item)
                item["routeId"] = canonical
                item["routeKey"] = route_key_for_routeid(canonical)
                rewritten += 1

            # Two favorites in one group cannot normally collapse onto the same
            # identity, because merged members have distinct directions and
            # pathId == direction. Guard anyway rather than write a document the
            # sync validator would reject.
            identity = _favorite_identity(item)
            if identity in seen:
                dropped += 1
                LOGGER.warning(
                    "dropping duplicate favorite after rewrite group=%s identity=%s",
                    group_name,
                    identity,
                )
                continue
            seen.add(identity)
            new_items.append(item)

        new_groups[group_name] = new_items

    return {**payload, "groups": new_groups}, rewritten, dropped


def _favorite_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    item_type = item.get("type", "boarding")
    if item_type == "route":
        return ("route", item.get("provider"), item.get("routeId"))
    if item_type == "station":
        return ("station", item.get("provider"), item.get("stationId"))
    return (
        "boarding",
        item.get("provider"),
        item.get("routeKey"),
        item.get("pathId"),
        item.get("stopId"),
    )


def migrate(settings: Settings, *, dry_run: bool = False) -> dict[str, int]:
    alias_index = load_route_alias_index(settings.db_path)
    if not len(alias_index):
        LOGGER.warning(
            "route_subroutes is empty in %s; run sync_static before migrating",
            settings.db_path,
        )
        return {"documents": 0, "rewritten": 0, "dropped": 0, "updated_documents": 0}

    stats = {"documents": 0, "rewritten": 0, "dropped": 0, "updated_documents": 0}
    now = int(time.time())

    with get_connection(settings.app_db_path) as connection:
        rows = connection.execute(
            """
            SELECT account_id, payload_json, revision
            FROM account_sync_documents
            WHERE namespace = 'favorites'
            ORDER BY account_id
            """
        ).fetchall()

        for row in rows:
            stats["documents"] += 1
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                LOGGER.warning("skipping unparseable favorites account_id=%s", row["account_id"])
                continue
            if not isinstance(payload, dict):
                continue

            # Only the ids this document actually uses need resolving.
            canonical_for = {
                routeid: alias_index.canonical(routeid)
                for routeid in _collect_routeids(payload)
                if alias_index.is_alias(routeid)
            }
            if not canonical_for:
                continue

            new_payload, rewritten, dropped = _rewrite_favorites_payload(payload, canonical_for)
            if rewritten == 0 and dropped == 0:
                continue

            stats["rewritten"] += rewritten
            stats["dropped"] += dropped
            stats["updated_documents"] += 1

            payload_json = _dump_payload_json(new_payload)
            LOGGER.info(
                "%s account_id=%s rewritten=%s dropped=%s revision=%s->%s",
                "would update" if dry_run else "updating",
                row["account_id"],
                rewritten,
                dropped,
                row["revision"],
                row["revision"] + 1,
            )
            if dry_run:
                continue

            with connection:
                connection.execute(
                    """
                    UPDATE account_sync_documents
                    SET payload_json = ?,
                        payload_size_bytes = ?,
                        content_hash = ?,
                        revision = revision + 1,
                        updated_at = ?,
                        last_synced_at = ?
                    WHERE account_id = ? AND namespace = 'favorites'
                    """,
                    (
                        payload_json,
                        len(payload_json.encode("utf-8")),
                        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                        now,
                        now,
                        row["account_id"],
                    ),
                )

    return stats


def _collect_routeids(payload: dict[str, Any]) -> set[str]:
    routeids: set[str] = set()
    groups = payload.get("groups")
    if not isinstance(groups, dict):
        return routeids
    for items in groups.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("type", "boarding") in {"route", "boarding"}
                and isinstance(item.get("routeId"), str)
            ):
                routeids.add(item["routeId"])
    return routeids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite saved favorites whose routeid was absorbed by a direction merge.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        settings = get_settings()
        stats = migrate(settings, dry_run=args.dry_run)
        LOGGER.info(
            "%s documents=%s updated_documents=%s rewritten=%s dropped=%s",
            "dry run complete" if args.dry_run else "migration complete",
            stats["documents"],
            stats["updated_documents"],
            stats["rewritten"],
            stats["dropped"],
        )
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
