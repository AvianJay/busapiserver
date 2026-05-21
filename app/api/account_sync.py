from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.account_sync_service import (
    SUPPORTED_SYNC_NAMESPACES,
    SyncApplyResult,
    SyncConflict,
    SyncConflictPolicy,
    SyncDocument,
    SyncNamespace,
    SyncValidationError,
    get_sync_document,
    list_sync_documents,
    normalize_client_modified_at,
    to_iso8601,
    upsert_sync_document,
)
from app.rate_limit import enforce_rate_limit, get_request_principal


router = APIRouter(tags=["account-sync"], dependencies=[Depends(enforce_rate_limit)])


class SyncUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    client_modified_at: str = Field(min_length=1, max_length=64)
    base_revision: int | None = Field(default=None, ge=1)
    base_etag: str | None = Field(default=None, min_length=1, max_length=200)
    conflict_policy: SyncConflictPolicy = "abort"
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/v1/account/sync")
def account_sync_status(request: Request, response: Response) -> dict[str, Any]:
    principal = _require_principal(request)
    documents = list_sync_documents(
        request.app.state.settings,
        account_id=principal.account_id,
    )
    _apply_no_store_header(response)
    return {
        "server_time": to_iso8601(_now_seconds()),
        "documents": {
            namespace: _document_payload(
                namespace=namespace,
                document=documents.get(namespace),
                include_payload=False,
            )
            for namespace in SUPPORTED_SYNC_NAMESPACES
        },
    }


@router.get("/api/v1/account/sync/{namespace}", response_model=None)
def account_sync_document(
    request: Request,
    response: Response,
    namespace: SyncNamespace,
) -> Response | dict[str, Any]:
    principal = _require_principal(request)
    document = get_sync_document(
        request.app.state.settings,
        account_id=principal.account_id,
        namespace=namespace,
    )
    if document is not None and _request_matches_etag(request, document):
        not_modified = Response(status_code=status.HTTP_304_NOT_MODIFIED)
        _apply_document_headers(not_modified, document)
        return not_modified

    if document is not None:
        _apply_document_headers(response, document)
    else:
        _apply_no_store_header(response)

    return _document_payload(
        namespace=namespace,
        document=document,
        include_payload=True,
    )


@router.put("/api/v1/account/sync/{namespace}")
async def put_account_sync_document(
    request: Request,
    response: Response,
    namespace: SyncNamespace,
) -> dict[str, Any]:
    principal = _require_principal(request)
    settings = request.app.state.settings
    raw_body = await request.body()
    if len(raw_body) > settings.account_sync_max_payload_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                "Sync request body exceeds the configured limit of "
                f"{settings.account_sync_max_payload_bytes} bytes."
            ),
        )

    try:
        payload = SyncUpsertRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    try:
        normalized_client_modified_at = normalize_client_modified_at(payload.client_modified_at)
        result = upsert_sync_document(
            settings,
            account_id=principal.account_id,
            namespace=namespace,
            schema_version=payload.schema_version,
            payload=payload.payload,
            client_modified_at=normalized_client_modified_at,
            conflict_policy=payload.conflict_policy,
            base_revision=payload.base_revision,
            base_etag=payload.base_etag,
        )
    except SyncValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result.status == "conflict":
        response.status_code = status.HTTP_409_CONFLICT
        if result.conflict is not None and result.conflict.current_document is not None:
            _apply_document_headers(response, result.conflict.current_document)
        else:
            _apply_no_store_header(response)
        return _conflict_payload(namespace, result.conflict_policy, result.conflict)

    if result.document is not None:
        _apply_document_headers(response, result.document)
    else:
        _apply_no_store_header(response)

    response.status_code = (
        status.HTTP_201_CREATED if result.status == "created" else status.HTTP_200_OK
    )
    return _write_result_payload(namespace, result)


def _require_principal(request: Request):
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return principal


def _document_payload(
    *,
    namespace: SyncNamespace,
    document: SyncDocument | None,
    include_payload: bool,
) -> dict[str, Any]:
    payload = {
        "namespace": namespace,
        "has_data": document is not None,
        "schema_version": (document.schema_version if document is not None else None),
        "revision": (document.revision if document is not None else 0),
        "etag": (document.etag if document is not None else None),
        "updated_at": (to_iso8601(document.updated_at) if document is not None else None),
        "last_synced_at": (
            to_iso8601(document.last_synced_at) if document is not None else None
        ),
        "last_client_modified_at": (
            document.last_client_modified_at if document is not None else None
        ),
        "payload_size_bytes": (
            document.payload_size_bytes if document is not None else 0
        ),
    }
    if include_payload:
        payload["payload"] = document.payload if document is not None else None
    return payload


def _write_result_payload(namespace: SyncNamespace, result: SyncApplyResult) -> dict[str, Any]:
    return {
        "ok": True,
        "status": result.status,
        "conflict_policy": result.conflict_policy,
        "document": _document_payload(
            namespace=namespace,
            document=result.document,
            include_payload=True,
        ),
    }


def _conflict_payload(
    namespace: SyncNamespace,
    conflict_policy: SyncConflictPolicy,
    conflict: SyncConflict | None,
) -> dict[str, Any]:
    if conflict is None:
        return {
            "ok": False,
            "status": "conflict",
            "conflict_policy": conflict_policy,
            "namespace": namespace,
            "message": "The sync request conflicts with newer server data.",
            "resolution_options": ["client_wins", "server_wins", "merge"],
            "server_document": _document_payload(
                namespace=namespace,
                document=None,
                include_payload=True,
            ),
            "merge_preview": {
                "status": "not_possible",
                "message": "No merge preview is available.",
                "payload": None,
            },
        }

    return {
        "ok": False,
        "status": "conflict",
        "conflict_policy": conflict_policy,
        "namespace": namespace,
        "message": conflict.message,
        "resolution_options": list(conflict.resolution_options),
        "server_document": _document_payload(
            namespace=namespace,
            document=conflict.current_document,
            include_payload=True,
        ),
        "merge_preview": {
            "status": conflict.merge_preview.status,
            "message": conflict.merge_preview.message,
            "payload": conflict.merge_preview.payload,
        },
    }


def _request_matches_etag(request: Request, document: SyncDocument) -> bool:
    if_none_match = (request.headers.get("if-none-match") or "").strip()
    if not if_none_match:
        return False
    if if_none_match == "*":
        return True
    candidates = [item.strip() for item in if_none_match.split(",") if item.strip()]
    return document.etag in candidates


def _apply_document_headers(response: Response, document: SyncDocument) -> None:
    response.headers["ETag"] = document.etag
    response.headers["Last-Modified"] = document.http_last_modified
    response.headers["X-YABUS-Sync-Revision"] = str(document.revision)
    _apply_no_store_header(response)


def _apply_no_store_header(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _now_seconds() -> int:
    import time

    return int(time.time())
