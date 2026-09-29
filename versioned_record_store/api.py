"""FastAPI 适配层：把可区分的失败原因映射为可区分的 HTTP 状态码。

状态码约定
----------
* 404 NOT_FOUND              记录不存在 / 游标对应记录已失效
* 409 VERSION_CONFLICT       乐观锁版本过期（丢失更新保护）
* 409 DUPLICATE_KEY          业务唯一键被占用（创建时）
* 409 RESTORE_CONFLICT       恢复与现存有效记录冲突
* 409 NOT_DELETED / 409 NOT_LIVE  状态不符（重复删除 / 恢复有效记录）
* 400 INVALID_CURSOR         游标损坏、签名错误或版本不支持
* 410 CURSOR_EXPIRED         游标曾合法但锚点已被物理清理

错误响应体统一为 ``{"error": {"code": ..., "message": ..., "detail": ...}}``，
客户端可以稳定地按 ``code`` 分支处理，而不必解析消息文本。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    ConcurrentDeleteError,
    DuplicateKeyError,
    ExpiredCursorError,
    InvalidCursorError,
    RecordNotFoundError,
    RestoreConflictError,
    StaleVersionError,
    StoreError,
    StoreMismatchError,
)
from .models import Page, Record, WriteOutcome
from .store import MAX_LIMIT, VersionedRecordStore

CONFLICT_CODES = {
    StaleVersionError: "VERSION_CONFLICT",
    DuplicateKeyError: "DUPLICATE_KEY",
    RestoreConflictError: "RESTORE_CONFLICT",
}


def _error(code: str, message: str, detail: dict[str, Any] | None = None,
           status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail or {}},
    )


def translate_store_error(exc: StoreError) -> HTTPException:
    """把存储层异常翻译为带稳定错误码的 HTTPException。"""
    if isinstance(exc, RecordNotFoundError):
        return _error("NOT_FOUND", str(exc),
                      {"record_id": exc.record_id}, status_code=404)

    if isinstance(exc, StaleVersionError):
        return _error("VERSION_CONFLICT", str(exc), {
            "record_id": exc.record_id,
            "expected_version": exc.expected_version,
            "current_version": exc.current_version,
        }, status_code=409)

    if isinstance(exc, DuplicateKeyError):
        return _error("DUPLICATE_KEY", str(exc), {
            "key": exc.key,
            "existing_record_id": exc.existing_record_id,
        }, status_code=409)

    if isinstance(exc, RestoreConflictError):
        return _error("RESTORE_CONFLICT", str(exc), {
            "record_id": exc.record_id,
            "key": exc.key,
            "existing_record_id": exc.existing_record_id,
        }, status_code=409)

    if isinstance(exc, ConcurrentDeleteError):
        code = "NOT_LIVE" if exc.actual_deleted else "NOT_DELETED"
        return _error(code, str(exc), {
            "record_id": exc.record_id,
            "expected_deleted": exc.expected_deleted,
            "actual_deleted": exc.actual_deleted,
        }, status_code=409)

    if isinstance(exc, ExpiredCursorError):
        return _error("CURSOR_EXPIRED", str(exc), {
            "anchor_seq": exc.anchor_seq,
            "retained_from_seq": exc.retained_from_seq,
        }, status_code=410)

    if isinstance(exc, (InvalidCursorError, StoreMismatchError)):
        code = "STORE_MISMATCH" if isinstance(exc, StoreMismatchError) \
            else "INVALID_CURSOR"
        return _error(code, str(exc), status_code=400)

    return _error("STORE_ERROR", str(exc), status_code=500)


# ----------------------------------------------------------------------
# 请求/响应模型
# ----------------------------------------------------------------------
class CreateRequest(BaseModel):
    key: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    record_id: str | None = None


class UpdateRequest(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=1)


class DeleteRequest(BaseModel):
    expected_version: int = Field(ge=1)


def _outcome_json(outcome: WriteOutcome) -> dict[str, Any]:
    return {
        "record_id": outcome.record_id,
        "key": outcome.key,
        "version": outcome.version,
        "seq": outcome.seq,
        "deleted": outcome.deleted,
    }


def _record_json(record: Record) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "key": record.key,
        "version": record.version,
        "seq": record.seq,
        "data": dict(record.data),
    }


def _page_json(page: Page) -> dict[str, Any]:
    return {
        "items": [_record_json(r) for r in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def create_router(store: VersionedRecordStore) -> APIRouter:
    """为给定存储实例构造一组 REST 路由。"""
    router = APIRouter(prefix="/records", tags=["records"])

    @router.post("", status_code=201)
    def create_record(req: CreateRequest) -> dict[str, Any]:
        try:
            outcome = store.create(req.key, req.data, req.record_id)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _outcome_json(outcome)

    @router.get("")
    def list_records(
        limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
        cursor: str | None = Query(default=None),
    ) -> dict[str, Any]:
        try:
            page = store.list_records(limit=limit, cursor=cursor)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _page_json(page)

    @router.get("/{record_id}")
    def get_record(record_id: str) -> dict[str, Any]:
        try:
            record = store.get(record_id)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _record_json(record)

    @router.put("/{record_id}")
    def update_record(record_id: str, req: UpdateRequest) -> dict[str, Any]:
        try:
            outcome = store.update(record_id, req.data, req.expected_version)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _outcome_json(outcome)

    @router.post("/{record_id}/delete")
    def delete_record(record_id: str, req: DeleteRequest) -> dict[str, Any]:
        try:
            outcome = store.delete(record_id, req.expected_version)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _outcome_json(outcome)

    @router.post("/{record_id}/restore")
    def restore_record(record_id: str, req: DeleteRequest) -> dict[str, Any]:
        try:
            outcome = store.restore(record_id, req.expected_version)
        except StoreError as exc:
            raise translate_store_error(exc) from exc
        return _outcome_json(outcome)

    return router


def error_handlers_app(app) -> Any:
    """可选：把 HTTPException 的响应体统一为 error 包裹结构。"""

    @app.exception_handler(HTTPException)
    async def handle_http_exc(_request, exc: HTTPException):  # noqa: ANN001
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = {"error": detail}
        else:
            body = {"error": {"code": "HTTP_ERROR",
                              "message": str(detail), "detail": {}}}
        return JSONResponse(status_code=exc.status_code, content=body)

    return app
