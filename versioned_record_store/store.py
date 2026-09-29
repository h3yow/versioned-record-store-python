"""线程安全的内存版本化记录存储。

并发模型：所有公共操作在同一把可重入锁内完成，因此每条写入对外要么
完整生效、要么完全不生效，不会出现“只落了一部分却返回成功”。

快照模型：每次成功的创建/更新/删除/恢复都会生成一个新的不可变快照
（含 active / all 两个作用域的冻结列表）。游标绑定快照 id，翻页期间
无论数据如何变化，同一游标看到的位置集合都不变，因此不会重复或漏读。
快照按 max_snapshots 有界保留，被淘汰后旧游标返回 CURSOR_EXPIRED。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from . import cursor as cursor_codec
from .errors import (
    AlreadyDeletedError,
    CursorExpiredError,
    InvalidCursorError,
    NotFoundError,
    RestoreKeyConflictError,
    DeletedRecordError,
    StoreClosedError,
    UniqueKeyConflictError,
    VersionConflictError,
)
from .models import ANY_VERSION, Page, Record

SCOPE_ACTIVE = "active"
SCOPE_ALL = "all"
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class _Snapshot:
    snapshot_id: int
    active: tuple[Record, ...]
    all: tuple[Record, ...]


def _scope_name(include_deleted: bool) -> str:
    return SCOPE_ALL if include_deleted else SCOPE_ACTIVE


class VersionedRecordStore:
    def __init__(
        self,
        *,
        max_snapshots: int = 64,
        secret: bytes | None = None,
    ) -> None:
        if max_snapshots < 1:
            raise ValueError("max_snapshots 必须 >= 1")
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._active_keys: dict[str, str] = {}  # 业务唯一键 -> 记录 id（仅有效记录）
        self._snapshots: dict[int, _Snapshot] = {}
        self._next_snapshot_id = 0
        self._next_record_seq = 0
        self._tick = 0
        self._max_snapshots = max_snapshots
        self._secret = secret if secret is not None else os.urandom(32)
        self._closed = False
        # 初始空快照，保证第一次读取也能绑定稳定快照
        self._publish_snapshot()

    # ---------- 生命周期 ----------

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self) -> "VersionedRecordStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise StoreClosedError("存储已关闭")

    # ---------- 内部工具（均持锁调用） ----------

    def _now(self) -> int:
        self._tick += 1
        return self._tick

    def _publish_snapshot(self) -> _Snapshot:
        all_records = tuple(
            sorted(self._records.values(), key=lambda r: (r.key, r.id))
        )
        active_records = tuple(r for r in all_records if not r.deleted)
        snap = _Snapshot(
            snapshot_id=self._next_snapshot_id,
            active=active_records,
            all=all_records,
        )
        self._snapshots[snap.snapshot_id] = snap
        self._next_snapshot_id += 1
        if len(self._snapshots) > self._max_snapshots:
            oldest = min(self._snapshots)
            del self._snapshots[oldest]
        return snap

    def _new_id(self) -> str:
        self._next_record_seq += 1
        return f"rec_{self._next_record_seq:06d}"

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key 必须是非空字符串")

    @staticmethod
    def _validate_data(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("data 必须是对象（dict）")
        return dict(data)

    def _require_record(self, record_id: str) -> Record:
        record = self._records.get(record_id)
        if record is None:
            raise NotFoundError(
                f"记录不存在: {record_id}",
                details={"id": record_id},
            )
        return record

    @staticmethod
    def _check_expected(record: Record, expected_version: int) -> None:
        if expected_version != ANY_VERSION and record.version != expected_version:
            raise VersionConflictError(
                "版本冲突：客户端版本已过期",
                details={
                    "id": record.id,
                    "expected_version": expected_version,
                    "current_version": record.version,
                },
            )

    # ---------- 写入 ----------

    def create(self, *, key: str, data: dict[str, Any]) -> Record:
        self._validate_key(key)
        payload = self._validate_data(data)
        with self._lock:
            self._check_open()
            existing_id = self._active_keys.get(key)
            if existing_id is not None:
                existing = self._records[existing_id]
                raise UniqueKeyConflictError(
                    f"业务唯一键已被有效记录占用: {key}",
                    details={
                        "key": key,
                        "existing_id": existing.id,
                        "existing_version": existing.version,
                    },
                )
            now = self._now()
            record = Record(
                id=self._new_id(),
                key=key,
                data=payload,
                version=1,
                deleted=False,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            self._records[record.id] = record
            self._active_keys[key] = record.id
            self._publish_snapshot()
            return record

    def update(
        self,
        record_id: str,
        *,
        expected_version: int,
        data: dict[str, Any] | None = None,
    ) -> Record:
        with self._lock:
            self._check_open()
            record = self._require_record(record_id)
            if record.deleted:
                raise DeletedRecordError(
                    f"记录已被软删除，需先恢复: {record_id}",
                    details={"id": record_id, "current_version": record.version},
                )
            self._check_expected(record, expected_version)
            if data is None:
                return record
            payload = self._validate_data(data)
            record = Record(
                id=record.id,
                key=record.key,
                data=payload,
                version=record.version + 1,
                deleted=False,
                created_at=record.created_at,
                updated_at=self._now(),
                deleted_at=None,
            )
            self._records[record.id] = record
            self._publish_snapshot()
            return record

    def delete(self, record_id: str, *, expected_version: int) -> Record:
        with self._lock:
            self._check_open()
            record = self._require_record(record_id)
            if record.deleted:
                raise AlreadyDeletedError(
                    f"记录已处于删除状态: {record_id}",
                    details={"id": record_id, "current_version": record.version},
                )
            self._check_expected(record, expected_version)
            record = Record(
                id=record.id,
                key=record.key,
                data=record.data,
                version=record.version + 1,
                deleted=True,
                created_at=record.created_at,
                updated_at=self._now(),
                deleted_at=self._tick,
            )
            self._records[record.id] = record
            # 软删除释放业务唯一键：此后允许用同 key 创建新记录
            if self._active_keys.get(record.key) == record.id:
                del self._active_keys[record.key]
            self._publish_snapshot()
            return record

    def restore(self, record_id: str, *, expected_version: int) -> Record:
        with self._lock:
            self._check_open()
            record = self._require_record(record_id)
            if not record.deleted:
                raise AlreadyDeletedError(
                    f"记录处于有效状态，无需恢复: {record_id}",
                    details={"id": record_id, "current_version": record.version},
                )
            self._check_expected(record, expected_version)
            holder_id = self._active_keys.get(record.key)
            if holder_id is not None and holder_id != record.id:
                holder = self._records[holder_id]
                raise RestoreKeyConflictError(
                    f"恢复失败：业务唯一键已被另一条有效记录占用: {record.key}",
                    details={
                        "id": record_id,
                        "key": record.key,
                        "conflicting_id": holder.id,
                        "conflicting_version": holder.version,
                    },
                )
            record = Record(
                id=record.id,
                key=record.key,
                data=record.data,
                version=record.version + 1,
                deleted=False,
                created_at=record.created_at,
                updated_at=self._now(),
                deleted_at=None,
            )
            self._records[record.id] = record
            self._active_keys[record.key] = record.id
            self._publish_snapshot()
            return record

    # ---------- 读取 ----------

    def get(self, record_id: str, *, include_deleted: bool = False) -> Record:
        with self._lock:
            self._check_open()
            record = self._require_record(record_id)
            if record.deleted and not include_deleted:
                raise DeletedRecordError(
                    f"记录已被软删除: {record_id}",
                    details={"id": record_id, "current_version": record.version},
                )
            return record

    def get_by_key(self, key: str, *, include_deleted: bool = False) -> Record:
        self._validate_key(key)
        with self._lock:
            self._check_open()
            record_id = self._active_keys.get(key)
            if record_id is not None:
                return self._records[record_id]
            if include_deleted:
                for record in self._records.values():
                    if record.key == key and record.deleted:
                        return record
            raise NotFoundError(
                f"没有使用该业务唯一键的有效记录: {key}",
                details={"key": key},
            )

    def list_page(
        self,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        include_deleted: bool = False,
    ) -> Page:
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ValueError("page_size 必须是整数")
        if page_size < 1 or page_size > _MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须在 1..{_MAX_PAGE_SIZE} 之间")

        scope = _scope_name(include_deleted)
        with self._lock:
            self._check_open()
            if cursor is None:
                snapshot = max(self._snapshots.values(), key=lambda s: s.snapshot_id)
                start = 0
            else:
                snapshot_id, last_key, last_id = cursor_codec.decode_cursor(
                    cursor, scope=scope, secret=self._secret
                )
                snapshot = self._snapshots.get(snapshot_id)
                if snapshot is None:
                    raise CursorExpiredError(
                        "游标对应的快照已失效（已被淘汰或存储已重建）",
                        details={"snapshot_id": snapshot_id},
                    )
                rows = snapshot.all if include_deleted else snapshot.active
                start = _index_of(rows, last_key, last_id)
                if start < 0:
                    raise InvalidCursorError(
                        "游标位置在其绑定的快照中不存在",
                        details={
                            "snapshot_id": snapshot_id,
                            "last_key": last_key,
                            "last_id": last_id,
                        },
                    )
                start += 1

            rows = snapshot.all if include_deleted else snapshot.active
            window = rows[start : start + page_size]
            has_more = start + page_size < len(rows)
            next_cursor: str | None = None
            if has_more and window:
                tail = window[-1]
                next_cursor = cursor_codec.encode_cursor(
                    snapshot_id=snapshot.snapshot_id,
                    last_key=tail.key,
                    last_id=tail.id,
                    scope=scope,
                    secret=self._secret,
                )
            return Page(
                items=window,
                next_cursor=next_cursor,
                snapshot_id=snapshot.snapshot_id,
                has_more=has_more,
            )


def _index_of(rows: tuple[Record, ...], key: str, record_id: str) -> int:
    for idx, row in enumerate(rows):
        if row.key == key and row.id == record_id:
            return idx
    return -1
