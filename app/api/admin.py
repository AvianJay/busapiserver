from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app.auth_service import (
    AuthError,
    admin_accounts_payload,
    revoke_account_tokens,
    update_account_role,
    validate_role,
)
from app.config import CITY_NAME_TO_PREFIX, INTERCITY_CITY_NAME
from app.rate_limit import enforce_rate_limit, get_request_principal


router = APIRouter(tags=["admin"], dependencies=[Depends(enforce_rate_limit)])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class AccountRoleUpdateRequest(BaseModel):
    role: str = Field(min_length=1)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        return validate_role(value)


class StaticSyncRequest(BaseModel):
    cities: list[str] | None = None
    force: bool = False

    @field_validator("cities")
    @classmethod
    def _validate_cities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            name = item.strip()
            if not name:
                continue
            # Only accept canonical TDX city names; anything else would be
            # silently ignored by sync_static and look like a no-op run.
            if name not in CITY_NAME_TO_PREFIX and name != INTERCITY_CITY_NAME:
                raise ValueError(f"Unknown city: {item!r}")
            if name not in cleaned:
                cleaned.append(name)
        return cleaned or None


@router.get("/admin/user_manage", include_in_schema=False, response_model=None)
def user_manage_page(request: Request) -> FileResponse | RedirectResponse:
    if get_request_principal(request) is None:
        return RedirectResponse("/auth", status_code=302)
    _require_admin(request)
    return FileResponse(WEB_ROOT / "user_manage.html", media_type="text/html")


@router.get("/api/v1/admin/users")
def list_users(request: Request) -> dict[str, object]:
    _require_admin(request)
    return admin_accounts_payload(request.app.state.settings)


@router.patch("/api/v1/admin/users/{account_id}")
def patch_user_role(
    account_id: str,
    request: Request,
    payload: AccountRoleUpdateRequest,
) -> dict[str, object]:
    _require_admin(request)
    try:
        return update_account_role(
            request.app.state.settings,
            account_id=int(account_id),
            role=payload.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid account id.") from exc
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/api/v1/admin/users/{account_id}/logout-all")
def logout_user_devices(
    account_id: str,
    request: Request,
) -> dict[str, object]:
    _require_admin(request)
    try:
        revoked_sessions = revoke_account_tokens(
            request.app.state.settings,
            account_id=int(account_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid account id.") from exc
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "ok": True,
        "account_id": account_id,
        "revoked_sessions": revoked_sessions,
    }


@router.post("/api/v1/admin/static-sync", status_code=202)
def trigger_static_sync(request: Request, payload: StaticSyncRequest | None = None) -> dict[str, object]:
    """Queue a static TDX sync without shell access.

    Returns immediately: a full sync takes many minutes. The work is handed to
    the weekly scheduler thread, so a manual run can never overlap the Monday
    04:00 run. Poll ``GET /api/v1/admin/static-sync`` for the outcome.
    """
    _require_admin(request)
    coordinator = getattr(request.app.state, "static_sync_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Static sync scheduler is not running.")

    body = payload or StaticSyncRequest()
    accepted = coordinator.request(
        cities=tuple(body.cities) if body.cities else None,
        force=body.force,
        source="admin",
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail="A static sync is already queued or running.",
        )
    return {"ok": True, "status": coordinator.status()}


@router.get("/api/v1/admin/static-sync")
def get_static_sync_status(request: Request) -> dict[str, object]:
    _require_admin(request)
    coordinator = getattr(request.app.state, "static_sync_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Static sync scheduler is not running.")
    return coordinator.status()


def _require_admin(request: Request) -> None:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
