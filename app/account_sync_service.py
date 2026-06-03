from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import formatdate
import hashlib
import json
import math
import re
import time
from typing import Any, Literal

from app.config import Settings
from app.db import get_connection


SyncNamespace = Literal["favorites", "preferences"]
SyncConflictPolicy = Literal["abort", "client_wins", "server_wins", "merge"]

SUPPORTED_SYNC_NAMESPACES: tuple[SyncNamespace, ...] = ("favorites", "preferences")
SUPPORTED_CONFLICT_POLICIES: tuple[SyncConflictPolicy, ...] = (
    "abort",
    "client_wins",
    "server_wins",
    "merge",
)

_INT64_MIN = -(2**63)
_INT64_MAX = (2**63) - 1
_FAVORITE_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FAVORITE_ALLOWED_KEYS = frozenset(
    {
        "provider",
        "routeKey",
        "pathId",
        "stopId",
        "routeId",
        "routeName",
        "stopName",
        "destinationPathId",
        "destinationStopId",
        "destinationStopName",
    }
)


class SyncValidationError(ValueError):
    """Raised when sync input is structurally invalid."""


@dataclass(frozen=True)
class SyncDocument:
    account_id: int
    namespace: str
    schema_version: int
    payload: dict[str, Any]
    payload_json: str
    payload_size_bytes: int
    content_hash: str
    revision: int
    created_at: int
    updated_at: int
    last_synced_at: int
    last_client_modified_at: str | None

    @property
    def etag(self) -> str:
        return f"\"sync-{self.namespace}-{self.revision}-{self.content_hash[:16]}\""

    @property
    def http_last_modified(self) -> str:
        return formatdate(self.updated_at, usegmt=True)


@dataclass(frozen=True)
class SyncMergePreview:
    status: Literal["possible", "not_possible"]
    message: str | None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class SyncConflict:
    namespace: str
    message: str
    current_document: SyncDocument | None
    resolution_options: tuple[SyncConflictPolicy, ...]
    merge_preview: SyncMergePreview


@dataclass(frozen=True)
class SyncApplyResult:
    status: Literal["created", "updated", "unchanged", "merged", "server_kept", "conflict"]
    document: SyncDocument | None
    conflict_policy: SyncConflictPolicy
    conflict: SyncConflict | None = None


def get_sync_document(
    settings: Settings,
    *,
    account_id: int,
    namespace: SyncNamespace,
) -> SyncDocument | None:
    with get_connection(settings.app_db_path) as connection:
        return _load_sync_document(connection, account_id=account_id, namespace=namespace)


def list_sync_documents(
    settings: Settings,
    *,
    account_id: int,
    namespaces: tuple[SyncNamespace, ...] = SUPPORTED_SYNC_NAMESPACES,
) -> dict[str, SyncDocument | None]:
    with get_connection(settings.app_db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                account_id,
                namespace,
                schema_version,
                payload_json,
                payload_size_bytes,
                content_hash,
                revision,
                created_at,
                updated_at,
                last_synced_at,
                last_client_modified_at
            FROM account_sync_documents
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchall()

    documents = {
        row["namespace"]: _row_to_sync_document(row)
        for row in rows
    }
    return {namespace: documents.get(namespace) for namespace in namespaces}


def upsert_sync_document(
    settings: Settings,
    *,
    account_id: int,
    namespace: SyncNamespace,
    schema_version: int,
    payload: dict[str, Any],
    client_modified_at: str,
    conflict_policy: SyncConflictPolicy = "abort",
    base_revision: int | None = None,
    base_etag: str | None = None,
    synced_at: int | None = None,
) -> SyncApplyResult:
    if schema_version < 1:
        raise SyncValidationError("schema_version must be at least 1.")
    if conflict_policy not in SUPPORTED_CONFLICT_POLICIES:
        raise SyncValidationError("Unsupported conflict policy.")

    normalized_payload = _normalize_document_payload(
        settings,
        namespace=namespace,
        payload=payload,
    )
    payload_json = _dump_payload_json(normalized_payload)
    payload_size_bytes = len(payload_json.encode("utf-8"))
    if payload_size_bytes > settings.account_sync_max_payload_bytes:
        raise SyncValidationError(
            "Payload exceeds the configured sync storage limit.",
        )

    normalized_client_modified_at = normalize_client_modified_at(client_modified_at)
    now = int(time.time()) if synced_at is None else int(synced_at)
    content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    with get_connection(settings.app_db_path) as connection:
        current = _load_sync_document(connection, account_id=account_id, namespace=namespace)
        base_provided = base_revision is not None or _normalize_optional_text(base_etag) is not None

        if current is not None and _documents_match(
            current,
            schema_version=schema_version,
            payload_json=payload_json,
        ):
            touched = _touch_last_synced_at(
                connection,
                account_id=account_id,
                namespace=namespace,
                last_synced_at=now,
            )
            if touched:
                connection.commit()
            return SyncApplyResult(
                status="unchanged",
                document=replace(current, last_synced_at=now),
                conflict_policy=conflict_policy,
            )

        if current is None:
            if base_provided and conflict_policy == "abort":
                return SyncApplyResult(
                    status="conflict",
                    document=None,
                    conflict_policy=conflict_policy,
                    conflict=_build_conflict(
                        settings,
                        namespace=namespace,
                        current=None,
                        incoming_payload=normalized_payload,
                        message="Server backup is missing or was replaced after the client's last sync.",
                    ),
                )
            if conflict_policy == "server_wins":
                return SyncApplyResult(
                    status="server_kept",
                    document=None,
                    conflict_policy=conflict_policy,
                )

            created = _write_sync_document(
                connection,
                account_id=account_id,
                namespace=namespace,
                schema_version=schema_version,
                payload=normalized_payload,
                payload_json=payload_json,
                payload_size_bytes=payload_size_bytes,
                content_hash=content_hash,
                created_at=now,
                updated_at=now,
                last_synced_at=now,
                last_client_modified_at=normalized_client_modified_at,
                revision=1,
            )
            connection.commit()
            return SyncApplyResult(
                status="created",
                document=created,
                conflict_policy=conflict_policy,
            )

        base_matches = _base_matches(current, base_revision=base_revision, base_etag=base_etag)
        if base_matches:
            updated = _write_sync_document(
                connection,
                account_id=account_id,
                namespace=namespace,
                schema_version=schema_version,
                payload=normalized_payload,
                payload_json=payload_json,
                payload_size_bytes=payload_size_bytes,
                content_hash=content_hash,
                created_at=current.created_at,
                updated_at=now,
                last_synced_at=now,
                last_client_modified_at=normalized_client_modified_at,
                revision=current.revision + 1,
            )
            connection.commit()
            return SyncApplyResult(
                status="updated",
                document=updated,
                conflict_policy=conflict_policy,
            )

        if conflict_policy == "client_wins":
            updated = _write_sync_document(
                connection,
                account_id=account_id,
                namespace=namespace,
                schema_version=schema_version,
                payload=normalized_payload,
                payload_json=payload_json,
                payload_size_bytes=payload_size_bytes,
                content_hash=content_hash,
                created_at=current.created_at,
                updated_at=now,
                last_synced_at=now,
                last_client_modified_at=normalized_client_modified_at,
                revision=current.revision + 1,
            )
            connection.commit()
            return SyncApplyResult(
                status="updated",
                document=updated,
                conflict_policy=conflict_policy,
            )

        if conflict_policy == "server_wins":
            _touch_last_synced_at(
                connection,
                account_id=account_id,
                namespace=namespace,
                last_synced_at=now,
            )
            connection.commit()
            return SyncApplyResult(
                status="server_kept",
                document=replace(current, last_synced_at=now),
                conflict_policy=conflict_policy,
            )

        if conflict_policy == "merge":
            merge_preview = _build_merge_preview(
                settings,
                namespace=namespace,
                current_payload=current.payload,
                incoming_payload=normalized_payload,
            )
            if merge_preview.status == "not_possible" or merge_preview.payload is None:
                return SyncApplyResult(
                    status="conflict",
                    document=current,
                    conflict_policy=conflict_policy,
                    conflict=SyncConflict(
                        namespace=namespace,
                        message="Server and client both changed this document and the changes could not be merged safely.",
                        current_document=current,
                        resolution_options=("client_wins", "server_wins", "merge"),
                        merge_preview=merge_preview,
                    ),
                )

            merged_payload = merge_preview.payload
            merged_payload_json = _dump_payload_json(merged_payload)
            merged_size_bytes = len(merged_payload_json.encode("utf-8"))
            if merged_size_bytes > settings.account_sync_max_payload_bytes:
                return SyncApplyResult(
                    status="conflict",
                    document=current,
                    conflict_policy=conflict_policy,
                    conflict=SyncConflict(
                        namespace=namespace,
                        message="The merged payload would exceed the configured sync storage limit.",
                        current_document=current,
                        resolution_options=("client_wins", "server_wins", "merge"),
                        merge_preview=SyncMergePreview(
                            status="not_possible",
                            message="Merged payload exceeds the configured size limit.",
                        ),
                    ),
                )

            if merged_payload_json == current.payload_json and schema_version == current.schema_version:
                _touch_last_synced_at(
                    connection,
                    account_id=account_id,
                    namespace=namespace,
                    last_synced_at=now,
                )
                connection.commit()
                return SyncApplyResult(
                    status="server_kept",
                    document=replace(current, last_synced_at=now),
                    conflict_policy=conflict_policy,
                )

            merged = _write_sync_document(
                connection,
                account_id=account_id,
                namespace=namespace,
                schema_version=schema_version,
                payload=merged_payload,
                payload_json=merged_payload_json,
                payload_size_bytes=merged_size_bytes,
                content_hash=hashlib.sha256(merged_payload_json.encode("utf-8")).hexdigest(),
                created_at=current.created_at,
                updated_at=now,
                last_synced_at=now,
                last_client_modified_at=normalized_client_modified_at,
                revision=current.revision + 1,
            )
            connection.commit()
            return SyncApplyResult(
                status="merged",
                document=merged,
                conflict_policy=conflict_policy,
            )

        return SyncApplyResult(
            status="conflict",
            document=current,
            conflict_policy=conflict_policy,
            conflict=_build_conflict(
                settings,
                namespace=namespace,
                current=current,
                incoming_payload=normalized_payload,
                message="Server data changed since the client's last known version.",
            ),
        )


def normalize_client_modified_at(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SyncValidationError("client_modified_at must not be empty.")

    parse_value = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise SyncValidationError(
            "client_modified_at must be an ISO 8601 timestamp.",
        ) from exc

    if parsed.tzinfo is None:
        raise SyncValidationError("client_modified_at must include timezone information.")

    return _isoformat_utc(parsed)


def to_iso8601(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return _isoformat_utc(datetime.fromtimestamp(timestamp, tz=timezone.utc))


def _base_matches(
    current: SyncDocument,
    *,
    base_revision: int | None,
    base_etag: str | None,
) -> bool:
    revision_matches = base_revision is None or base_revision == current.revision
    normalized_etag = _normalize_optional_text(base_etag)
    etag_matches = normalized_etag is None or normalized_etag == current.etag
    return revision_matches and etag_matches


def _build_conflict(
    settings: Settings,
    *,
    namespace: SyncNamespace,
    current: SyncDocument | None,
    incoming_payload: dict[str, Any],
    message: str,
) -> SyncConflict:
    merge_preview = _build_merge_preview(
        settings,
        namespace=namespace,
        current_payload=(current.payload if current is not None else _empty_payload(namespace)),
        incoming_payload=incoming_payload,
    )
    return SyncConflict(
        namespace=namespace,
        message=message,
        current_document=current,
        resolution_options=("client_wins", "server_wins", "merge"),
        merge_preview=merge_preview,
    )


def _build_merge_preview(
    settings: Settings,
    *,
    namespace: SyncNamespace,
    current_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
) -> SyncMergePreview:
    try:
        merged = _merge_payloads(
            settings,
            namespace=namespace,
            current_payload=current_payload,
            incoming_payload=incoming_payload,
        )
    except SyncValidationError as exc:
        return SyncMergePreview(status="not_possible", message=str(exc))

    return SyncMergePreview(status="possible", message=None, payload=merged)


def _merge_payloads(
    settings: Settings,
    *,
    namespace: SyncNamespace,
    current_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
) -> dict[str, Any]:
    if namespace == "favorites":
        return _merge_favorites_payloads(settings, current_payload, incoming_payload)
    if namespace == "preferences":
        return _merge_preferences_payloads(settings, current_payload, incoming_payload)
    raise SyncValidationError("Unsupported sync namespace.")


def _merge_favorites_payloads(
    settings: Settings,
    current_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
) -> dict[str, Any]:
    current_groups = current_payload.get("groups", {})
    incoming_groups = incoming_payload.get("groups", {})
    merged_groups: dict[str, list[dict[str, Any]]] = {}

    ordered_group_names = list(current_groups.keys()) + [
        name for name in incoming_groups.keys() if name not in current_groups
    ]

    for group_name in ordered_group_names:
        server_items = current_groups.get(group_name, [])
        client_items = incoming_groups.get(group_name, [])
        merged_items = [dict(item) for item in server_items]
        positions = {
            _favorite_identity_key(item): index
            for index, item in enumerate(server_items)
        }

        for client_item in client_items:
            item_key = _favorite_identity_key(client_item)
            if item_key not in positions:
                merged_items.append(dict(client_item))
                positions[item_key] = len(merged_items) - 1
                continue

            merged_item = _merge_favorite_item(
                server_items[positions[item_key]],
                client_item,
            )
            merged_items[positions[item_key]] = merged_item

        if merged_items:
            merged_groups[group_name] = merged_items

    return _normalize_favorites_payload(settings, {"groups": merged_groups})


def _merge_favorite_item(server_item: dict[str, Any], client_item: dict[str, Any]) -> dict[str, Any]:
    merged = dict(server_item)
    for field_name in (
        "routeId",
        "routeName",
        "stopName",
        "destinationPathId",
        "destinationStopId",
        "destinationStopName",
    ):
        server_value = server_item.get(field_name)
        client_value = client_item.get(field_name)
        if server_value == client_value:
            continue
        if server_value is None:
            merged[field_name] = client_value
            continue
        if client_value is None:
            continue
        raise SyncValidationError(
            f"Favorite metadata conflict on field '{field_name}'.",
        )

    if merged.get("destinationStopId") is None:
        merged.pop("destinationPathId", None)
        merged.pop("destinationStopName", None)

    return merged


def _merge_preferences_payloads(
    settings: Settings,
    current_payload: dict[str, Any],
    incoming_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = _merge_json_objects(
        current_payload,
        incoming_payload,
        path="preferences",
    )
    return _normalize_preferences_payload(settings, merged)


def _merge_json_objects(
    server_value: dict[str, Any],
    client_value: dict[str, Any],
    *,
    path: str,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    ordered_keys = list(server_value.keys()) + [
        key for key in client_value.keys() if key not in server_value
    ]

    for key in ordered_keys:
        server_present = key in server_value
        client_present = key in client_value
        server_item = server_value.get(key)
        client_item = client_value.get(key)
        next_path = f"{path}.{key}"

        if server_present and not client_present:
            merged[key] = server_item
            continue
        if client_present and not server_present:
            merged[key] = client_item
            continue
        if server_item == client_item:
            merged[key] = server_item
            continue
        if isinstance(server_item, dict) and isinstance(client_item, dict):
            merged[key] = _merge_json_objects(server_item, client_item, path=next_path)
            continue
        raise SyncValidationError(
            f"Preference conflict at '{next_path}'.",
        )

    return merged


def _normalize_document_payload(
    settings: Settings,
    *,
    namespace: SyncNamespace,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if namespace == "favorites":
        return _normalize_favorites_payload(settings, payload)
    if namespace == "preferences":
        return _normalize_preferences_payload(settings, payload)
    raise SyncValidationError("Unsupported sync namespace.")


def _normalize_favorites_payload(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SyncValidationError("Favorites payload must be a JSON object.")

    extra_keys = sorted(set(payload) - {"groups"})
    if extra_keys:
        raise SyncValidationError(
            f"Favorites payload contains unsupported keys: {', '.join(extra_keys)}.",
        )

    groups_value = payload.get("groups")
    if groups_value is None:
        groups_value = {}
    if not isinstance(groups_value, dict):
        raise SyncValidationError("Favorites payload 'groups' must be an object.")

    normalized_groups: dict[str, list[dict[str, Any]]] = {}
    seen_group_names: set[str] = set()
    total_favorites = 0

    for raw_group_name, raw_items in groups_value.items():
        group_name = _normalize_group_name(
            raw_group_name,
            max_length=settings.account_sync_max_group_name_length,
        )
        if group_name in seen_group_names:
            raise SyncValidationError(f"Duplicate favorite group '{group_name}'.")
        seen_group_names.add(group_name)

        if not isinstance(raw_items, list):
            raise SyncValidationError(
                f"Favorite group '{group_name}' must contain a list of favorites.",
            )

        normalized_items: list[dict[str, Any]] = []
        seen_items: set[tuple[str, int, int, int]] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise SyncValidationError(
                    f"Favorite group '{group_name}' contains a non-object favorite.",
                )
            normalized_item = _normalize_favorite_item(raw_item)
            item_key = _favorite_identity_key(normalized_item)
            if item_key in seen_items:
                raise SyncValidationError(
                    f"Favorite group '{group_name}' contains duplicate favorites.",
                )
            seen_items.add(item_key)
            normalized_items.append(normalized_item)

        if normalized_items:
            normalized_groups[group_name] = normalized_items
            total_favorites += len(normalized_items)

    if total_favorites > settings.account_sync_max_favorites:
        raise SyncValidationError(
            f"Favorites exceed the maximum of {settings.account_sync_max_favorites} items.",
        )

    return {"groups": normalized_groups}


def _normalize_favorite_item(payload: dict[str, Any]) -> dict[str, Any]:
    extra_keys = sorted(set(payload) - _FAVORITE_ALLOWED_KEYS)
    if extra_keys:
        raise SyncValidationError(
            f"Favorite item contains unsupported keys: {', '.join(extra_keys)}.",
        )

    provider = _normalize_provider(payload.get("provider"))
    route_key = _require_int(payload.get("routeKey"), field_name="routeKey", minimum=1)
    path_id = _require_int(payload.get("pathId"), field_name="pathId", minimum=0)
    stop_id = _require_int(payload.get("stopId"), field_name="stopId", minimum=1)
    route_id = _normalize_optional_text(
        payload.get("routeId"),
        field_name="routeId",
        max_length=64,
    )
    route_name = _normalize_optional_text(
        payload.get("routeName"),
        field_name="routeName",
        max_length=120,
    )
    stop_name = _normalize_optional_text(
        payload.get("stopName"),
        field_name="stopName",
        max_length=120,
    )
    destination_stop_id = _optional_int(payload.get("destinationStopId"), field_name="destinationStopId", minimum=1)
    destination_path_id = _optional_int(payload.get("destinationPathId"), field_name="destinationPathId", minimum=0)
    destination_stop_name = _normalize_optional_text(
        payload.get("destinationStopName"),
        field_name="destinationStopName",
        max_length=120,
    )

    if destination_stop_id is None:
        if destination_path_id is not None or destination_stop_name is not None:
            raise SyncValidationError(
                "Favorite destinationPathId/destinationStopName require destinationStopId.",
            )
    elif destination_path_id is None:
        destination_path_id = path_id

    normalized = {
        "provider": provider,
        "routeKey": route_key,
        "pathId": path_id,
        "stopId": stop_id,
    }
    if route_id is not None:
        normalized["routeId"] = route_id
    if route_name is not None:
        normalized["routeName"] = route_name
    if stop_name is not None:
        normalized["stopName"] = stop_name
    if destination_stop_id is not None:
        normalized["destinationPathId"] = destination_path_id
        normalized["destinationStopId"] = destination_stop_id
        if destination_stop_name is not None:
            normalized["destinationStopName"] = destination_stop_name

    return normalized


def _normalize_preferences_payload(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SyncValidationError("Preferences payload must be a JSON object.")
    return _validate_json_object(
        payload,
        settings=settings,
        path="preferences",
        depth=1,
    )


def _validate_json_object(
    value: dict[str, Any],
    *,
    settings: Settings,
    path: str,
    depth: int,
) -> dict[str, Any]:
    if depth > settings.account_sync_max_json_depth:
        raise SyncValidationError(
            f"{path} exceeds the maximum supported nesting depth.",
        )

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise SyncValidationError(f"{path} contains a non-string key.")
        if not raw_key.strip():
            raise SyncValidationError(f"{path} contains an empty key.")
        normalized[raw_key] = _validate_json_value(
            raw_value,
            settings=settings,
            path=f"{path}.{raw_key}",
            depth=depth + 1,
        )
    return normalized


def _validate_json_value(
    value: Any,
    *,
    settings: Settings,
    path: str,
    depth: int,
) -> Any:
    if depth > settings.account_sync_max_json_depth:
        raise SyncValidationError(
            f"{path} exceeds the maximum supported nesting depth.",
        )
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < _INT64_MIN or value > _INT64_MAX:
            raise SyncValidationError(f"{path} is outside the supported integer range.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SyncValidationError(f"{path} must be a finite number.")
        return value
    if isinstance(value, list):
        return [
            _validate_json_value(
                item,
                settings=settings,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return _validate_json_object(
            value,
            settings=settings,
            path=path,
            depth=depth,
        )
    raise SyncValidationError(f"{path} must be valid JSON data.")


def _documents_match(
    current: SyncDocument,
    *,
    schema_version: int,
    payload_json: str,
) -> bool:
    return current.schema_version == schema_version and current.payload_json == payload_json


def _write_sync_document(
    connection,
    *,
    account_id: int,
    namespace: str,
    schema_version: int,
    payload: dict[str, Any],
    payload_json: str,
    payload_size_bytes: int,
    content_hash: str,
    created_at: int,
    updated_at: int,
    last_synced_at: int,
    last_client_modified_at: str | None,
    revision: int,
) -> SyncDocument:
    connection.execute(
        """
        INSERT INTO account_sync_documents (
            account_id,
            namespace,
            schema_version,
            payload_json,
            payload_size_bytes,
            content_hash,
            revision,
            created_at,
            updated_at,
            last_synced_at,
            last_client_modified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, namespace) DO UPDATE SET
            schema_version = excluded.schema_version,
            payload_json = excluded.payload_json,
            payload_size_bytes = excluded.payload_size_bytes,
            content_hash = excluded.content_hash,
            revision = excluded.revision,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            last_synced_at = excluded.last_synced_at,
            last_client_modified_at = excluded.last_client_modified_at
        """,
        (
            account_id,
            namespace,
            schema_version,
            payload_json,
            payload_size_bytes,
            content_hash,
            revision,
            created_at,
            updated_at,
            last_synced_at,
            last_client_modified_at,
        ),
    )
    return SyncDocument(
        account_id=account_id,
        namespace=namespace,
        schema_version=schema_version,
        payload=payload,
        payload_json=payload_json,
        payload_size_bytes=payload_size_bytes,
        content_hash=content_hash,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
        last_synced_at=last_synced_at,
        last_client_modified_at=last_client_modified_at,
    )


def _touch_last_synced_at(
    connection,
    *,
    account_id: int,
    namespace: str,
    last_synced_at: int,
) -> bool:
    cursor = connection.execute(
        """
        UPDATE account_sync_documents
        SET last_synced_at = ?
        WHERE account_id = ? AND namespace = ?
        """,
        (last_synced_at, account_id, namespace),
    )
    return cursor.rowcount > 0


def _load_sync_document(connection, *, account_id: int, namespace: str) -> SyncDocument | None:
    row = connection.execute(
        """
        SELECT
            account_id,
            namespace,
            schema_version,
            payload_json,
            payload_size_bytes,
            content_hash,
            revision,
            created_at,
            updated_at,
            last_synced_at,
            last_client_modified_at
        FROM account_sync_documents
        WHERE account_id = ? AND namespace = ?
        """,
        (account_id, namespace),
    ).fetchone()
    if row is None:
        return None
    return _row_to_sync_document(row)


def _row_to_sync_document(row) -> SyncDocument:
    payload_json = row["payload_json"]
    return SyncDocument(
        account_id=int(row["account_id"]),
        namespace=row["namespace"],
        schema_version=int(row["schema_version"]),
        payload=json.loads(payload_json),
        payload_json=payload_json,
        payload_size_bytes=int(row["payload_size_bytes"]),
        content_hash=row["content_hash"],
        revision=int(row["revision"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        last_synced_at=int(row["last_synced_at"]),
        last_client_modified_at=row["last_client_modified_at"],
    )


def _dump_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _favorite_identity_key(item: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        item["provider"],
        int(item["routeKey"]),
        int(item["pathId"]),
        int(item["stopId"]),
    )


def _normalize_provider(value: Any) -> str:
    if not isinstance(value, str):
        raise SyncValidationError("Favorite provider must be a string.")
    normalized = value.strip().lower()
    if not normalized:
        raise SyncValidationError("Favorite provider must not be empty.")
    if not _FAVORITE_PROVIDER_RE.fullmatch(normalized):
        raise SyncValidationError("Favorite provider format is invalid.")
    return normalized


def _normalize_group_name(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise SyncValidationError("Favorite group names must be strings.")
    normalized = value.strip()
    if not normalized:
        raise SyncValidationError("Favorite group names must not be empty.")
    if len(normalized) > max_length:
        raise SyncValidationError(
            f"Favorite group names must be at most {max_length} characters long.",
        )
    return normalized


def _require_int(value: Any, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncValidationError(f"Favorite field '{field_name}' must be an integer.")
    if value < minimum:
        raise SyncValidationError(
            f"Favorite field '{field_name}' must be at least {minimum}.",
        )
    return value


def _optional_int(value: Any, *, field_name: str, minimum: int) -> int | None:
    if value is None:
        return None
    return _require_int(value, field_name=field_name, minimum=minimum)


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str = "value",
    max_length: int | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SyncValidationError(f"Field '{field_name}' must be a string.")
    normalized = value.strip()
    if not normalized:
        return None
    if max_length is not None and len(normalized) > max_length:
        raise SyncValidationError(
            f"Field '{field_name}' exceeds the maximum length of {max_length}.",
        )
    return normalized


def _empty_payload(namespace: SyncNamespace) -> dict[str, Any]:
    if namespace == "favorites":
        return {"groups": {}}
    return {}


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
