from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.fcm import announcement_push_public_config, register_announcement_push_token
from app.rate_limit import enforce_rate_limit


router = APIRouter(tags=["push"], dependencies=[Depends(enforce_rate_limit)])


class PushTokenRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    platform: str = Field(min_length=1)

    @field_validator("token", "platform")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field must not be empty.")
        return normalized


@router.get("/api/v1/push/public-config")
def public_push_config(request: Request) -> dict:
    return announcement_push_public_config(request.app.state.settings)


@router.post("/api/v1/push/fcm-token")
def register_push_token(
    request: Request,
    payload: PushTokenRegisterRequest,
) -> dict[str, bool]:
    try:
        register_announcement_push_token(
            request.app.state.settings.db_path,
            token=payload.token,
            platform=payload.platform,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}
