from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from app.config import Settings
from app.db import get_connection


_PUSH_PLATFORMS = ("android", "web")
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def register_announcement_push_token(
    db_path: str | Path,
    *,
    token: str,
    platform: str,
    user_agent: str | None,
) -> None:
    normalized_token = token.strip()
    normalized_platform = platform.strip().lower()
    if not normalized_token:
        raise ValueError("Push token must not be empty.")
    if normalized_platform not in _PUSH_PLATFORMS:
        raise ValueError("Unsupported push platform.")

    now = int(time.time())
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO announcement_push_tokens (
                token,
                platform,
                user_agent,
                created_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                platform = excluded.platform,
                user_agent = excluded.user_agent,
                last_seen_at = excluded.last_seen_at
            """,
            (
                normalized_token,
                normalized_platform,
                (user_agent or "").strip(),
                now,
                now,
            ),
        )
        connection.commit()


def announcement_push_public_config(settings: Settings) -> dict[str, Any]:
    web = {
        "apiKey": settings.fcm_web_api_key,
        "authDomain": settings.fcm_web_auth_domain,
        "projectId": settings.fcm_project_id,
        "storageBucket": settings.fcm_web_storage_bucket,
        "messagingSenderId": settings.fcm_web_messaging_sender_id,
        "appId": settings.fcm_web_app_id,
        "measurementId": settings.fcm_web_measurement_id,
        "vapidKey": settings.fcm_web_vapid_key,
    }
    return {
        "enabled": _has_service_account_credentials(settings),
        "app_base_url": settings.app_public_base_url,
        "project_id": settings.fcm_project_id,
        "web_enabled": _web_push_is_configured(settings),
        "web": web,
    }


def send_announcement_push(
    settings: Settings,
    announcement: dict[str, Any],
) -> dict[str, Any]:
    push_platforms = _target_push_platforms(announcement)
    if not push_platforms:
        return {
            "attempted": True,
            "sent": 0,
            "failed": 0,
            "invalidated": 0,
            "skipped": True,
            "reason": "Announcement targets do not include android or web.",
        }

    if not _has_service_account_credentials(settings):
        return {
            "attempted": True,
            "sent": 0,
            "failed": 0,
            "invalidated": 0,
            "skipped": True,
            "reason": "FCM service account credentials are not configured.",
        }

    tokens = _load_push_tokens(settings.db_path, platforms=push_platforms)
    if not tokens:
        return {
            "attempted": True,
            "sent": 0,
            "failed": 0,
            "invalidated": 0,
            "skipped": True,
            "reason": "No registered push tokens matched this announcement.",
        }

    access_token, auth_error = _build_access_token(settings)
    if access_token is None:
        return {
            "attempted": True,
            "sent": 0,
            "failed": len(tokens),
            "invalidated": 0,
            "skipped": False,
            "reason": auth_error or "Could not build an FCM access token.",
        }

    endpoint = (
        f"https://fcm.googleapis.com/v1/projects/"
        f"{settings.fcm_project_id}/messages:send"
    )
    invalid_tokens: list[str] = []
    errors: list[dict[str, str]] = []
    sent = 0
    failed = 0

    for entry in tokens:
        payload = {"message": _build_fcm_message(settings, announcement, entry)}
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            failed += 1
            errors.append(
                {
                    "token": entry["token"],
                    "platform": entry["platform"],
                    "error": str(exc),
                }
            )
            continue

        if response.ok:
            sent += 1
            continue

        failed += 1
        error_code, error_message = _extract_fcm_error(response)
        errors.append(
            {
                "token": entry["token"],
                "platform": entry["platform"],
                "error": error_message,
                "code": error_code,
            }
        )
        if error_code == "UNREGISTERED":
            invalid_tokens.append(entry["token"])

    if invalid_tokens:
        _delete_push_tokens(settings.db_path, invalid_tokens)

    result: dict[str, Any] = {
        "attempted": True,
        "sent": sent,
        "failed": failed,
        "invalidated": len(invalid_tokens),
        "skipped": False,
    }
    if errors:
        result["errors"] = errors[:10]
    return result


def _build_fcm_message(
    settings: Settings,
    announcement: dict[str, Any],
    token_entry: dict[str, str],
) -> dict[str, Any]:
    announcement_id = f"{announcement.get('id') or ''}".strip()
    title = f"{announcement.get('title') or 'YABus'}".strip() or "YABus"
    body = f"{announcement.get('content') or ''}".strip()
    link = (
        f"{settings.app_public_base_url}/"
        f"{_announcement_detail_path(announcement_id).lstrip('/')}"
    )
    platform = token_entry["platform"]
    data = {
        "announcement_id": announcement_id,
        "title": title,
        "content": body,
        "link": link,
    }
    message: dict[str, Any] = {
        "token": token_entry["token"],
        "data": data,
    }

    if platform == "android":
        message["notification"] = {
            "title": title,
            "body": body,
        }
        message["android"] = {
            "priority": "high",
            "notification": {
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
        }
    elif platform == "web":
        message["webpush"] = {
            "headers": {"Urgency": "high"},
            "fcm_options": {"link": link},
            "notification": {
                "title": title,
                "body": body,
                "icon": f"{settings.app_public_base_url}/icons/Icon-192.png",
                "data": {
                    "announcementId": announcement_id,
                    "link": link,
                },
            },
        }

    return message


def _target_push_platforms(announcement: dict[str, Any]) -> tuple[str, ...]:
    targets = announcement.get("targets")
    if not isinstance(targets, dict):
        return _PUSH_PLATFORMS

    platforms = targets.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        return _PUSH_PLATFORMS

    normalized = []
    for item in platforms:
        platform = f"{item or ''}".strip().lower()
        if platform in _PUSH_PLATFORMS and platform not in normalized:
            normalized.append(platform)
    return tuple(normalized)


def _announcement_detail_path(announcement_id: str) -> str:
    return f"/announcement/{announcement_id}"


def _load_push_tokens(
    db_path: str | Path,
    *,
    platforms: tuple[str, ...],
) -> list[dict[str, str]]:
    placeholders = ", ".join("?" for _ in platforms)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT token, platform
            FROM announcement_push_tokens
            WHERE platform IN ({placeholders})
            ORDER BY last_seen_at DESC, created_at DESC
            """,
            platforms,
        ).fetchall()
    return [{"token": row["token"], "platform": row["platform"]} for row in rows]


def _delete_push_tokens(db_path: str | Path, tokens: list[str]) -> None:
    placeholders = ", ".join("?" for _ in tokens)
    with get_connection(db_path) as connection:
        connection.execute(
            f"DELETE FROM announcement_push_tokens WHERE token IN ({placeholders})",
            tokens,
        )
        connection.commit()


def _build_access_token(settings: Settings) -> tuple[str | None, str | None]:
    info = _load_service_account_info(settings)
    if info is None:
        return None, "FCM service account credentials are not configured."

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ImportError:
        return None, "google-auth is not installed on the server."

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=[_FCM_SCOPE],
        )
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        return None, f"FCM credential refresh failed: {exc}"

    token = credentials.token
    if not token:
        return None, "FCM credential refresh returned no access token."
    return token, None


def _load_service_account_info(settings: Settings) -> dict[str, Any] | None:
    raw_json = settings.fcm_service_account_json
    if raw_json and raw_json.strip():
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError:
            return None
        return _normalize_service_account_info(info)

    raw_path = (settings.fcm_service_account_json_path or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _normalize_service_account_info(info)


def _normalize_service_account_info(info: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


def _has_service_account_credentials(settings: Settings) -> bool:
    if settings.fcm_service_account_json and settings.fcm_service_account_json.strip():
        return True
    raw_path = (settings.fcm_service_account_json_path or "").strip()
    return bool(raw_path and Path(raw_path).exists())


def _web_push_is_configured(settings: Settings) -> bool:
    return all(
        (
            settings.fcm_web_api_key,
            settings.fcm_web_auth_domain,
            settings.fcm_project_id,
            settings.fcm_web_storage_bucket,
            settings.fcm_web_messaging_sender_id,
            settings.fcm_web_app_id,
            settings.fcm_web_vapid_key,
        )
    )


def _extract_fcm_error(response: requests.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", f"HTTP {response.status_code}"

    error = payload.get("error")
    if not isinstance(error, dict):
        return "", f"HTTP {response.status_code}"

    code = f"{error.get('status') or ''}".strip().upper()
    message = f"{error.get('message') or ''}".strip() or f"HTTP {response.status_code}"
    return code, message
