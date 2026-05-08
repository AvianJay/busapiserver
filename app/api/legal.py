from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit


router = APIRouter(tags=["legal"], dependencies=[Depends(enforce_rate_limit)])

LOGGER = get_logger("legal")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
TERMS_OF_SERVICE_PATH = PROJECT_ROOT / "TERMS.md"
PRIVACY_POLICY_PATH = PROJECT_ROOT / "PRIVACY.md"


@router.get("/terms-of-service", include_in_schema=False)
def terms_of_service_page() -> FileResponse:
    return _page_response("terms-of-service.html")


@router.get("/privacy-policy", include_in_schema=False)
def privacy_policy_page() -> FileResponse:
    return _page_response("privacy-policy.html")


@router.get("/api/v1/terms-of-service")
def get_terms_of_service() -> dict[str, str]:
    return {"content": _read_markdown_document(TERMS_OF_SERVICE_PATH)}


@router.get("/api/v1/privacy-policy")
def get_privacy_policy() -> dict[str, str]:
    return {"content": _read_markdown_document(PRIVACY_POLICY_PATH)}


def _read_markdown_document(path: Path) -> str:
    resolved_path = path.resolve()
    try:
        last_modified_ns = resolved_path.stat().st_mtime_ns
    except OSError as exc:
        LOGGER.warning("failed to stat legal document path=%s error=%s", resolved_path, exc)
        raise HTTPException(status_code=500, detail="Legal document is unavailable.") from exc

    try:
        return _read_markdown_document_cached(str(resolved_path), last_modified_ns)
    except OSError as exc:
        LOGGER.warning("failed to read legal document path=%s error=%s", resolved_path, exc)
        raise HTTPException(status_code=500, detail="Legal document is unavailable.") from exc


@lru_cache(maxsize=4)
def _read_markdown_document_cached(path: str, last_modified_ns: int) -> str:
    del last_modified_ns
    return Path(path).read_text(encoding="utf-8")


def _page_response(name: str) -> FileResponse:
    return FileResponse(WEB_ROOT / name, media_type="text/html")