from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth_service import generate_snowflake
from app.db import get_connection
from app.rate_limit import check_rate_limit, enforce_rate_limit, get_request_principal
from app.request_analytics import MAX_USER_AGENT_LENGTH, parse_user_agent


router = APIRouter(tags=["feedback"], dependencies=[Depends(enforce_rate_limit)])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
_FEEDBACK_LIMIT_REQUESTS = 1
_FEEDBACK_LIMIT_WINDOW_SECONDS = 60
_FEEDBACK_LIMIT_DETAIL = "Feedback submissions are limited to 1 per minute."


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("title", "content")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field must not be empty.")
        return normalized


@router.get("/admin/feedbacks", include_in_schema=False, response_model=None)
def feedbacks_page(request: Request) -> FileResponse | RedirectResponse:
    if get_request_principal(request) is None:
        return RedirectResponse("/auth", status_code=302)
    _require_admin(request)
    return FileResponse(WEB_ROOT / "feedbacks.html", media_type="text/html")


@router.post("/api/v1/feedback", status_code=status.HTTP_201_CREATED)
def create_feedback(request: Request, payload: FeedbackCreateRequest) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    check_rate_limit(
        f"user:{principal.account_id}",
        "feedback-submit",
        requests=_FEEDBACK_LIMIT_REQUESTS,
        window_seconds=_FEEDBACK_LIMIT_WINDOW_SECONDS,
        detail=_FEEDBACK_LIMIT_DETAIL,
    )

    settings = request.app.state.settings
    now = int(time.time())
    feedback_id = generate_snowflake(settings)
    user_agent = (request.headers.get("user-agent") or "").strip()
    parsed_user_agent = parse_user_agent(user_agent)
    safe_user_agent = user_agent[:MAX_USER_AGENT_LENGTH]

    with get_connection(settings.app_db_path) as connection:
        connection.execute(
            """
            INSERT INTO feedbacks (
                id,
                account_id,
                title,
                content,
                created_at,
                client_family,
                platform_name,
                system_name,
                system_version,
                app_version,
                app_commit_hash,
                browser_name,
                browser_version,
                user_agent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_id,
                principal.account_id,
                payload.title,
                payload.content,
                now,
                parsed_user_agent.client_family,
                parsed_user_agent.platform_name,
                parsed_user_agent.system_name,
                parsed_user_agent.system_version,
                parsed_user_agent.app_version,
                parsed_user_agent.app_commit_hash,
                parsed_user_agent.browser_name,
                parsed_user_agent.browser_version,
                safe_user_agent,
            ),
        )
        connection.commit()

    return {
        "ok": True,
        "feedback_id": str(feedback_id),
        "created_at": now,
    }


@router.get("/api/v1/admin/feedbacks")
def list_feedbacks(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    _require_admin(request)

    now = int(time.time())
    with get_connection(request.app.state.settings.app_db_path) as connection:
        summary_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COUNT(DISTINCT account_id) AS account_count,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent_24h_count,
                MAX(created_at) AS last_created_at
            FROM feedbacks
            """,
            (now - 86400,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT
                f.id,
                f.account_id,
                a.role,
                f.title,
                f.content,
                f.created_at,
                f.client_family,
                f.platform_name,
                f.system_name,
                f.system_version,
                f.app_version,
                f.app_commit_hash,
                f.browser_name,
                f.browser_version,
                f.user_agent,
                (
                    SELECT COALESCE(
                        NULLIF(i.display_name, ''),
                        NULLIF(i.email, ''),
                        i.provider || ':' || i.provider_user_id
                    )
                    FROM account_oauth_identities i
                    WHERE i.account_id = f.account_id
                    ORDER BY
                        CASE i.provider
                            WHEN 'google' THEN 0
                            WHEN 'discord' THEN 1
                            ELSE 2
                        END,
                        i.updated_at DESC,
                        i.created_at DESC
                    LIMIT 1
                ) AS account_label,
                (
                    SELECT COALESCE(NULLIF(i.email, ''), '')
                    FROM account_oauth_identities i
                    WHERE i.account_id = f.account_id
                    ORDER BY
                        CASE i.provider
                            WHEN 'google' THEN 0
                            WHEN 'discord' THEN 1
                            ELSE 2
                        END,
                        i.updated_at DESC,
                        i.created_at DESC
                    LIMIT 1
                ) AS account_email
            FROM feedbacks f
            JOIN accounts a ON a.id = f.account_id
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return {
        "summary": {
            "total_count": int(summary_row["total_count"] if summary_row else 0),
            "account_count": int(summary_row["account_count"] if summary_row else 0),
            "recent_24h_count": int(
                (summary_row["recent_24h_count"] if summary_row else 0) or 0
            ),
            "last_created_at": summary_row["last_created_at"] if summary_row else None,
        },
        "feedbacks": [_row_to_feedback(row) for row in rows],
    }


def _row_to_feedback(row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "account_id": str(row["account_id"]),
        "account_role": row["role"],
        "account_label": row["account_label"] or "",
        "account_email": row["account_email"] or "",
        "title": row["title"],
        "content": row["content"],
        "created_at": int(row["created_at"]),
        "client_family": row["client_family"],
        "platform_name": row["platform_name"],
        "system_name": row["system_name"],
        "system_version": row["system_version"],
        "app_version": row["app_version"],
        "app_commit_hash": row["app_commit_hash"],
        "browser_name": row["browser_name"],
        "browser_version": row["browser_version"],
        "user_agent": row["user_agent"],
    }


def _require_admin(request: Request) -> None:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
