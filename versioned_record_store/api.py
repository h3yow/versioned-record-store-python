"""FastAPI HTTP 接口。

路由：
  POST   /records                  创建
  GET    /records                  分页浏览（cursor, page_size, include_deleted）
  GET    /records/{id}             读取（include_deleted）
  PUT    /records/{id}             条件更新（expected_version）
  DELETE /records/{id}             软删除（expected_version）
  POST   /records/{id}/restore     恢复（expected_version）

所有 StoreError 统一映射为结构化 JSON 错误体，不同原因对应不同状态码。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ErrorCode, StoreError
from .models import ANY_VERSION
from .store import VersionedRecordStore

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DELETED_RECORD: 410,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.UNIQUE_KEY_CONFLICT: 409,
    ErrorCode.RESTORE_KEY_CONFLICT: 409,
    ErrorCode.ALREADY_DELETED: 409,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.CURSOR_EXPIRED: 410,
    ErrorCode.CURSOR_SCOPE_MISMATCH: 400,
    ErrorCode.MALFORMED_CURSOR: 400,
    ErrorCode.STORE_CLOSED: 503,
}


class CreateBody(BaseModel):
    key: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class UpdateBody(BaseModel):
    expected_version: int = Field(default=ANY_VERSION)
    data: dict[str, Any]


class MutateParams(BaseModel):
    expected_version: int = ANY_VERSION


def create_app(store: VersionedRecordStore | None = None) -> FastAPI:
    app = FastAPI(
        title="versioned-record-store",
        version="0.1.0",
    )
    app.state.store = store or VersionedRecordStore()

    @app.exception_handler(StoreError)
    async def _on_store_error(_request: Request, exc: StoreError) -> JSONResponse:
        status = _STATUS_BY_CODE.get(exc.error_code, 400)
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.post("/records", status_code=201)
    async def create_record(body: CreateBody) -> dict[str, Any]:
        return app.state.store.create(key=body.key, data=body.data).to_dict()

    @app.get("/records")
    async def list_records(
        page_size: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
    ) -> dict[str, Any]:
        page = app.state.store.list_page(
            page_size=page_size,
            cursor=cursor,
            include_deleted=include_deleted,
        )
        return {
            "items": [r.to_dict() for r in page.items],
            "next_cursor": page.next_cursor,
            "snapshot_id": page.snapshot_id,
            "has_more": page.has_more,
        }

    @app.get("/records/{record_id}")
    async def get_record(
        record_id: str, include_deleted: bool = Query(default=False)
    ) -> dict[str, Any]:
        return app.state.store.get(
            record_id, include_deleted=include_deleted
        ).to_dict()

    @app.put("/records/{record_id}")
    async def update_record(record_id: str, body: UpdateBody) -> dict[str, Any]:
        return app.state.store.update(
            record_id,
            expected_version=body.expected_version,
            data=body.data,
        ).to_dict()

    @app.delete("/records/{record_id}")
    async def delete_record(
        record_id: str,
        expected_version: int = Query(..., description="客户端已知版本号"),
    ) -> dict[str, Any]:
        return app.state.store.delete(
            record_id, expected_version=expected_version
        ).to_dict()

    @app.post("/records/{record_id}/restore")
    async def restore_record(
        record_id: str, body: MutateParams | None = None
    ) -> dict[str, Any]:
        expected = body.expected_version if body is not None else ANY_VERSION
        return app.state.store.restore(
            record_id, expected_version=expected
        ).to_dict()

    return app
