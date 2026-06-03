"""Discord webhook notifications.

These notifications deliberately carry only metadata and a link back to the
admin dashboard. Raw user-submitted text (feedback title/content) is never
included in the webhook payload, so malicious or abusive input cannot reach
Discord and put the server's account at risk. Reviewers open the admin page to
read the actual content in a controlled environment.
"""

from __future__ import annotations

from typing import Any

import requests

from app.config import Settings
from app.logging_utils import get_logger


LOGGER = get_logger("discord_webhook")

_DISCORD_TIMEOUT_SECONDS = 10
# Discord brand-ish blue for the embed sidebar.
_EMBED_COLOR = 0x5865F2


def _post_webhook(webhook_url: str, payload: dict[str, Any]) -> bool:
    try:
        response = requests.post(webhook_url, json=payload, timeout=_DISCORD_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        LOGGER.warning("discord webhook request failed: %s", exc)
        return False

    if not response.ok:
        LOGGER.warning(
            "discord webhook returned non-ok status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return False
    return True


def notify_new_feedback(
    settings: Settings,
    *,
    feedback_id: int,
    account_id: int,
    account_role: str | None,
    created_at: int,
    title_length: int,
    content_length: int,
    client_family: str | None,
    platform_name: str | None,
    app_version: str | None,
) -> bool:
    """Send a metadata-only Discord notification about a new feedback entry.

    Returns True if the webhook was delivered successfully, False otherwise
    (including when no webhook URL is configured). Never raises.
    """
    webhook_url = settings.feedback_discord_webhook_url
    if not webhook_url:
        return False

    admin_url = f"{settings.auth_public_base_url}/admin/feedbacks"

    # NOTE: Only non-sensitive metadata is included here. The raw user-provided
    # title/content are intentionally omitted; we only report their lengths so a
    # reviewer can gauge size before opening the dashboard.
    fields = [
        {"name": "Feedback ID", "value": str(feedback_id), "inline": True},
        {"name": "Account", "value": f"{account_id} ({account_role or 'user'})", "inline": True},
        {"name": "Title length", "value": str(title_length), "inline": True},
        {"name": "Content length", "value": str(content_length), "inline": True},
    ]
    if client_family:
        platform_label = client_family
        if platform_name:
            platform_label = f"{client_family} / {platform_name}"
        if app_version:
            platform_label = f"{platform_label} (v{app_version})"
        fields.append({"name": "Client", "value": platform_label, "inline": True})

    payload = {
        "username": "YABus Feedback",
        "embeds": [
            {
                "title": "📝 New feedback received",
                "description": f"[Open admin dashboard to review]({admin_url})",
                "color": _EMBED_COLOR,
                "fields": fields,
                "timestamp": _iso8601(created_at),
                "footer": {"text": "Content withheld — review in admin dashboard."},
            }
        ],
        # Defense-in-depth: never allow any mentions to ping, even if some field
        # ever contained an @everyone/role-like string.
        "allowed_mentions": {"parse": []},
    }

    return _post_webhook(webhook_url, payload)


def _iso8601(epoch_seconds: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
