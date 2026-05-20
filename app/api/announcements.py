from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db import get_connection
from app.fcm import send_announcement_push
from app.rate_limit import enforce_rate_limit, get_request_principal
from app.request_analytics import parse_user_agent


router = APIRouter(tags=["announcements"], dependencies=[Depends(enforce_rate_limit)])

_ALLOWED_BEHAVIORS = {"none", "once", "forever"}
_ALLOWED_PLATFORMS = {"android", "ios", "windows", "macos", "linux", "web"}
_ALLOWED_EDITOR_ROLES = {"mod", "admin"}
_VERSION_PART_PATTERN = re.compile(r"^(>=|<=|>|<|==|=)?\s*(\d+(?:\.\d+)*)$")


class AnnouncementBehaviorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    red_dot: str = Field(default="once")
    popup: str = Field(default="once")

    @field_validator("red_dot", "popup")
    @classmethod
    def _validate_behavior(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_BEHAVIORS:
            raise ValueError("Behavior must be one of: none, once, forever.")
        return normalized


class AnnouncementTargetsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[str] | None = None
    version_constraint: str | None = None

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for item in value:
            platform = item.strip().lower()
            if platform not in _ALLOWED_PLATFORMS:
                raise ValueError("Unsupported announcement target platform.")
            if platform not in normalized:
                normalized.append(platform)
        return normalized or None

    @field_validator("version_constraint")
    @classmethod
    def _validate_version_constraint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        _parse_version_constraint(normalized)
        return normalized


class AnnouncementEmbedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    url: str = Field(min_length=1)

    @field_validator("type", "url")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field must not be empty.")
        return normalized


class AnnouncementActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    label: str = Field(min_length=1)
    url: str = Field(min_length=1)

    @field_validator("type", "label", "url")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field must not be empty.")
        return normalized


class AnnouncementCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_type: str = Field(default="markdown")
    author: str | None = None
    created_at: int | None = None
    expire_at: int | None = None
    behavior: AnnouncementBehaviorPayload = Field(
        default_factory=AnnouncementBehaviorPayload,
    )
    targets: AnnouncementTargetsPayload | None = None
    sound_url: str | None = None
    embed: AnnouncementEmbedPayload | None = None
    actions: list[AnnouncementActionPayload] | None = None
    send_push_notification: bool = False

    @field_validator("id", "title", "content", "content_type", "author", "sound_url")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str | None) -> str:
        if value is None:
            return "markdown"
        normalized = value.strip().lower()
        if normalized != "markdown":
            raise ValueError("Only markdown announcements are supported.")
        return normalized

    @model_validator(mode="after")
    def _validate_times(self) -> AnnouncementCreateRequest:
        created_at = self.created_at
        expire_at = self.expire_at
        if expire_at is not None and created_at is not None and expire_at <= created_at:
            raise ValueError("expire_at must be later than created_at.")
        return self


class AnnouncementUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    content: str | None = None
    content_type: str | None = None
    author: str | None = None
    created_at: int | None = None
    expire_at: int | None = None
    behavior: AnnouncementBehaviorPayload | None = None
    targets: AnnouncementTargetsPayload | None = None
    sound_url: str | None = None
    embed: AnnouncementEmbedPayload | None = None
    actions: list[AnnouncementActionPayload] | None = None

    @field_validator("title", "content", "content_type", "author", "sound_url")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized != "markdown":
            raise ValueError("Only markdown announcements are supported.")
        return normalized

    @model_validator(mode="after")
    def _validate_update(self) -> AnnouncementUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        created_at = self.created_at
        expire_at = self.expire_at
        if expire_at is not None and created_at is not None and expire_at <= created_at:
            raise ValueError("expire_at must be later than created_at.")
        return self


@router.get("/announcements", include_in_schema=False, response_model=None)
def legacy_announcements_page() -> RedirectResponse:
    return RedirectResponse("/admin/announcements", status_code=302)


@router.get("/admin/announcements", include_in_schema=False, response_model=None)
def announcements_page(request: Request) -> FileResponse | RedirectResponse:
    from pathlib import Path

    principal = get_request_principal(request)
    if principal is None:
        return RedirectResponse("/auth", status_code=302)
    if principal.role not in _ALLOWED_EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Moderator access required.")
    return FileResponse(
        Path(__file__).resolve().parents[1] / "web" / "announcements.html",
        media_type="text/html",
    )


@router.get("/api/v1/announcements/all")
def list_all_announcements(request: Request) -> list[dict[str, Any]]:
    """Return ALL announcements including expired (mod/admin only)."""
    _require_editor(request)
    with get_connection(request.app.state.settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id, title, content, content_type, author,
                created_at, expire_at, behavior_json, targets_json,
                sound_url, embed_json, actions_json
            FROM announcements
            ORDER BY created_at DESC, id DESC
            """,
        ).fetchall()
    return [_row_to_announcement(row) for row in rows]


@router.get("/api/v1/announcements")
def list_announcements(
    request: Request,
    platform: str = "",
    version: str = "",
) -> list[dict[str, Any]]:
    resolved_platform, resolved_version = _resolve_client_target(
        request,
        platform=platform,
        version=version,
    )
    now = int(time.time())
    with get_connection(request.app.state.settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                content,
                content_type,
                author,
                created_at,
                expire_at,
                behavior_json,
                targets_json,
                sound_url,
                embed_json,
                actions_json
            FROM announcements
            WHERE expire_at IS NULL OR expire_at > ?
            ORDER BY created_at DESC, id DESC
            """,
            (now,),
        ).fetchall()

    announcements = [_row_to_announcement(row) for row in rows]
    return [
        announcement
        for announcement in announcements
        if _matches_targets(
            announcement,
            platform=resolved_platform,
            version=resolved_version,
        )
    ]


@router.post("/api/v1/announcements", status_code=status.HTTP_201_CREATED)
def create_announcement(
    request: Request,
    payload: AnnouncementCreateRequest,
) -> dict[str, Any]:
    principal = _require_editor(request)
    now = int(time.time())
    announcement = {
        "id": payload.id or uuid4().hex,
        "title": payload.title,
        "content": payload.content,
        "content_type": payload.content_type,
        "author": payload.author or principal.role,
        "created_at": payload.created_at or now,
        "expire_at": payload.expire_at,
        "behavior": payload.behavior.model_dump(mode="json"),
        "targets": payload.targets.model_dump(mode="json") if payload.targets else None,
        "sound_url": payload.sound_url,
        "embed": payload.embed.model_dump(mode="json") if payload.embed else None,
        "actions": [
            action.model_dump(mode="json") for action in payload.actions or []
        ]
        or None,
    }
    _validate_announcement_payload(announcement)

    with get_connection(request.app.state.settings.db_path) as connection:
        existing = connection.execute(
            "SELECT 1 FROM announcements WHERE id = ?",
            (announcement["id"],),
        ).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Announcement already exists.")
        _upsert_announcement(connection, announcement, updated_at=now)
        connection.commit()
    if not payload.send_push_notification:
        return announcement

    push_result = send_announcement_push(request.app.state.settings, announcement)
    return {
        **announcement,
        "push_notification": push_result,
    }


@router.patch("/api/v1/announcements/{announcement_id}")
def update_announcement(
    announcement_id: str,
    request: Request,
    payload: AnnouncementUpdateRequest,
) -> dict[str, Any]:
    _require_editor(request)
    now = int(time.time())
    with get_connection(request.app.state.settings.db_path) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                title,
                content,
                content_type,
                author,
                created_at,
                expire_at,
                behavior_json,
                targets_json,
                sound_url,
                embed_json,
                actions_json
            FROM announcements
            WHERE id = ?
            """,
            (announcement_id.strip(),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Announcement not found.")

        announcement = _row_to_announcement(row)
        updated = _apply_update(announcement, payload)
        _validate_announcement_payload(updated)
        _upsert_announcement(connection, updated, updated_at=now)
        connection.commit()
    return updated


def _require_editor(request: Request):
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if principal.role not in _ALLOWED_EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Moderator access required.")
    return principal


def _resolve_client_target(
    request: Request,
    *,
    platform: str,
    version: str,
) -> tuple[str | None, str | None]:
    normalized_platform = _normalize_platform(platform)
    normalized_version = _normalize_optional_text(version)
    if normalized_platform is not None and normalized_version is not None:
        return normalized_platform, normalized_version

    parsed = parse_user_agent(request.headers.get("user-agent"))
    return (
        normalized_platform or _normalize_platform(parsed.platform_name),
        normalized_version or _normalize_optional_text(parsed.app_version),
    )


def _apply_update(
    announcement: dict[str, Any],
    payload: AnnouncementUpdateRequest,
) -> dict[str, Any]:
    updated = dict(announcement)
    for field_name in payload.model_fields_set:
        value = getattr(payload, field_name)
        if field_name == "behavior":
            updated[field_name] = value.model_dump(mode="json") if value else announcement["behavior"]
            continue
        if field_name == "targets":
            updated[field_name] = value.model_dump(mode="json") if value else None
            continue
        if field_name == "embed":
            updated[field_name] = value.model_dump(mode="json") if value else None
            continue
        if field_name == "actions":
            updated[field_name] = [action.model_dump(mode="json") for action in value or []] or None
            continue
        updated[field_name] = value
    return updated


def _validate_announcement_payload(announcement: dict[str, Any]) -> None:
    created_at = int(announcement["created_at"])
    expire_at = announcement.get("expire_at")
    if expire_at is not None and int(expire_at) <= created_at:
        raise HTTPException(status_code=422, detail="expire_at must be later than created_at.")

    targets = announcement.get("targets")
    if targets is not None:
        version_constraint = targets.get("version_constraint")
        if version_constraint:
            _parse_version_constraint(version_constraint)


def _matches_targets(
    announcement: dict[str, Any],
    *,
    platform: str | None,
    version: str | None,
) -> bool:
    targets = announcement.get("targets")
    if not isinstance(targets, dict):
        return True

    platforms = targets.get("platforms")
    if platform is not None and isinstance(platforms, list) and platforms:
        if platform not in platforms:
            return False

    version_constraint = targets.get("version_constraint")
    if version is not None and isinstance(version_constraint, str) and version_constraint.strip():
        return _matches_version_constraint(version, version_constraint)

    return True


def _upsert_announcement(
    connection,
    announcement: dict[str, Any],
    *,
    updated_at: int,
) -> None:
    connection.execute(
        """
        INSERT INTO announcements (
            id,
            title,
            content,
            content_type,
            author,
            created_at,
            expire_at,
            behavior_json,
            targets_json,
            sound_url,
            embed_json,
            actions_json,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            content = excluded.content,
            content_type = excluded.content_type,
            author = excluded.author,
            created_at = excluded.created_at,
            expire_at = excluded.expire_at,
            behavior_json = excluded.behavior_json,
            targets_json = excluded.targets_json,
            sound_url = excluded.sound_url,
            embed_json = excluded.embed_json,
            actions_json = excluded.actions_json,
            updated_at = excluded.updated_at
        """,
        (
            announcement["id"],
            announcement["title"],
            announcement["content"],
            announcement["content_type"],
            announcement.get("author"),
            int(announcement["created_at"]),
            announcement.get("expire_at"),
            json.dumps(announcement.get("behavior") or {}, ensure_ascii=False),
            _json_or_none(announcement.get("targets")),
            announcement.get("sound_url"),
            _json_or_none(announcement.get("embed")),
            _json_or_none(announcement.get("actions")),
            updated_at,
        ),
    )


def _row_to_announcement(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "content_type": row["content_type"],
        "author": row["author"],
        "created_at": row["created_at"],
        "expire_at": row["expire_at"],
        "behavior": _json_loads(row["behavior_json"]) or {
            "red_dot": "once",
            "popup": "once",
        },
        "targets": _json_loads(row["targets_json"]),
        "sound_url": row["sound_url"],
        "embed": _json_loads(row["embed_json"]),
        "actions": _json_loads(row["actions_json"]),
    }


def _json_loads(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_platform(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if normalized not in _ALLOWED_PLATFORMS:
        return None
    return normalized


def _parse_version_constraint(constraint: str) -> list[tuple[str, tuple[int, ...]]]:
    parsed_parts: list[tuple[str, tuple[int, ...]]] = []
    for raw_part in constraint.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = _VERSION_PART_PATTERN.match(part)
        if match is None:
            raise HTTPException(status_code=422, detail="Invalid version_constraint.")
        operator = match.group(1) or "="
        parsed_parts.append((operator, _parse_version(match.group(2))))
    if not parsed_parts:
        raise HTTPException(status_code=422, detail="Invalid version_constraint.")
    return parsed_parts


def _parse_version(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.strip().split("."))


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    left_parts = left + (0,) * (size - len(left))
    right_parts = right + (0,) * (size - len(right))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def _matches_version_constraint(version: str, constraint: str) -> bool:
    current = _parse_version(version)
    for operator, expected in _parse_version_constraint(constraint):
        comparison = _compare_versions(current, expected)
        if operator == ">=" and comparison < 0:
            return False
        if operator == ">" and comparison <= 0:
            return False
        if operator == "<=" and comparison > 0:
            return False
        if operator == "<" and comparison >= 0:
            return False
        if operator in {"=", "=="} and comparison != 0:
            return False
    return True
