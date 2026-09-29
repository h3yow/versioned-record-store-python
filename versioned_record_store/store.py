"""版本化记录存储核心实现（线程安全、内存态）。

并发模型
--------
所有变更都在同一把可重入锁内完成"检查 + 写入"，因此：

* 乐观锁（expected_version）检查与版本推进是原子的，并发写入只会
  有一个成功，其余得到 :class:`StaleVersionError`，不会丢失更新；
* 唯一键检查与登记是原子的，并发的删除/恢复/创建不会产生两条同时
  有效且业务键相同的记录；
* 任何冲突都在状态被修改 *之前* 抛出，要么完整生效、要么完全不变，
  不存在只落一部分却返回成功。

顺序与版本
----------
* ``seq``：记录首次创建时分配的单调递增序号，终生不变，作为游标
  分页的稳定顺序键 ``(seq, record_id)``；
* ``version``：从 1 开始，每次 update/delete/restore 都 +1。
  条件写入必须携带客户端已知版本，不匹配即拒绝。

删除语义
--------
删除是软删除：记录与全部历史版本保留，但从有效集合与分页结果中
消失，并释放业务唯一键；恢复时版本继续 +1，若业务键已被其他有效
记录占用则抛 :class:`RestoreConflictError`。
"""

from __future__ import annotations

import copy
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .cursor import CursorAnchor, decode_cursor, encode_cursor
from .errors import (
    ConcurrentDeleteError,
    DuplicateKeyError,
    ExpiredCursorError,
    RecordNotFoundError,
    RestoreConflictError,
    StaleVersionError,
)
from .models import Page, Record, RecordSnapshot, WriteOutcome

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


@dataclass(slots=True)
class _Revision:
    """单次修订的不可变内容（版本历史中的一项）。"""

    version: int
    data: Mapping[str, Any]
    deleted: bool


@dataclass(slots=True)
class _Entry:
    """一条记录的完整内部状态（含全部历史版本）。"""

    record_id: str
    key: str
    seq: int
    version: int
    data: Mapping[str, Any]
    deleted: bool
    history: list[_Revision] = field(default_factory=list)


class VersionedRecordStore:
    """线程安全的版本化记录存储（内存实现，进程内可复现）。"""

    def __init__(self, store_id: str | None = None) -> None:
        self._store_id = store_id if store_id is not None else uuid.uuid4().hex
        self._secret = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        # 仅登记"有效"记录占用的业务键：key -> record_id
        self._live_keys: dict[str, str] = {}
        self._next_seq = 1
        # 游标失效水位：锚点 seq 小于该值的旧游标一律拒绝
        self._retained_from_seq = 0

    @property
    def store_id(self) -> str:
        return self._store_id

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _copy_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("data must be a mapping")
        # 深拷贝：写入边界隔离调用方后续对嵌套结构的修改；
        # 读取边界同样使用，保证对外暴露的快照真正不可变。
        return copy.deepcopy(dict(payload))

    def _require_entry(self, record_id: str) -> _Entry:
        entry = self._entries.get(record_id)
        if entry is None:
            raise RecordNotFoundError(record_id)
        return entry

    def _check_version(self, entry: _Entry, expected_version: int) -> None:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise TypeError("expected_version must be an int")
        if expected_version != entry.version:
            raise StaleVersionError(
                entry.record_id, expected_version, entry.version
            )

    def _to_record(self, entry: _Entry) -> Record:
        return Record(
            record_id=entry.record_id,
            key=entry.key,
            version=entry.version,
            data=self._copy_data(entry.data),
            seq=entry.seq,
        )

    def _to_snapshot(self, entry: _Entry, revision: _Revision) -> RecordSnapshot:
        return RecordSnapshot(
            record_id=entry.record_id,
            key=entry.key,
            version=revision.version,
            data=self._copy_data(revision.data),
            seq=entry.seq,
            deleted=revision.deleted,
            store_id=self._store_id,
        )

    def _outcome(self, entry: _Entry) -> WriteOutcome:
        return WriteOutcome(
            record_id=entry.record_id,
            key=entry.key,
            version=entry.version,
            seq=entry.seq,
            deleted=entry.deleted,
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create(
        self,
        key: str,
        data: Mapping[str, Any],
        record_id: str | None = None,
    ) -> WriteOutcome:
        """创建一条有效记录（version=1）。

        业务键已被有效记录占用时抛 :class:`DuplicateKeyError`；
        显式提供的 record_id 已存在时同样抛 :class:`DuplicateKeyError`。
        """
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        new_id = record_id if record_id is not None else uuid.uuid4().hex
        payload = self._copy_data(data)

        with self._lock:
            if new_id in self._entries:
                # record_id 绝不复用（即使旧记录已删除），保证历史可追溯
                raise DuplicateKeyError(
                    f"record_id {new_id!r} already exists "
                    f"(record ids are never reused)",
                    new_id,
                )
            holder = self._live_keys.get(key)
            if holder is not None:
                raise DuplicateKeyError(key, holder)

            seq = self._next_seq
            self._next_seq += 1
            entry = _Entry(
                record_id=new_id,
                key=key,
                seq=seq,
                version=1,
                data=payload,
                deleted=False,
                history=[_Revision(version=1, data=payload, deleted=False)],
            )
            self._entries[new_id] = entry
            self._live_keys[key] = new_id
            return self._outcome(entry)

    def update(
        self,
        record_id: str,
        data: Mapping[str, Any],
        expected_version: int,
    ) -> WriteOutcome:
        """基于客户端已知版本更新记录，version 前进 1。

        * 记录不存在 -> :class:`RecordNotFoundError`
        * 版本过期 -> :class:`StaleVersionError`
        * 记录已被软删除 -> :class:`ConcurrentDeleteError`
        """
        payload = self._copy_data(data)
        with self._lock:
            entry = self._require_entry(record_id)
            self._check_version(entry, expected_version)
            if entry.deleted:
                # 更新要求记录处于有效状态
                raise ConcurrentDeleteError(
                    record_id, expected_deleted=False, actual_deleted=True
                )

            entry.version += 1
            entry.data = payload
            entry.history.append(
                _Revision(version=entry.version, data=payload, deleted=False)
            )
            return self._outcome(entry)

    def delete(self, record_id: str, expected_version: int) -> WriteOutcome:
        """软删除（version+1），并释放业务唯一键。

        * 版本过期 -> :class:`StaleVersionError`
        * 记录已是删除态 -> :class:`ConcurrentDeleteError`
          （并发删除/恢复的可区分结果）
        """
        with self._lock:
            entry = self._require_entry(record_id)
            self._check_version(entry, expected_version)
            if entry.deleted:
                raise ConcurrentDeleteError(
                    record_id, expected_deleted=False, actual_deleted=True
                )

            entry.deleted = True
            entry.version += 1
            entry.history.append(
                _Revision(version=entry.version, data=entry.data, deleted=True)
            )
            # 唯一键登记的移除必须与状态变更同处一个临界区
            if self._live_keys.get(entry.key) == record_id:
                del self._live_keys[entry.key]
            return self._outcome(entry)

    def restore(self, record_id: str, expected_version: int) -> WriteOutcome:
        """恢复软删除记录（version+1），重新占用业务唯一键。

        * 版本过期 -> :class:`StaleVersionError`
        * 记录本就有效 -> :class:`ConcurrentDeleteError`
        * 业务键已被其他有效记录占用 -> :class:`RestoreConflictError`
        """
        with self._lock:
            entry = self._require_entry(record_id)
            self._check_version(entry, expected_version)
            if not entry.deleted:
                raise ConcurrentDeleteError(
                    record_id, expected_deleted=True, actual_deleted=False
                )

            holder = self._live_keys.get(entry.key)
            if holder is not None and holder != record_id:
                raise RestoreConflictError(record_id, entry.key, holder)

            entry.deleted = False
            entry.version += 1
            entry.history.append(
                _Revision(version=entry.version, data=entry.data, deleted=False)
            )
            self._live_keys[entry.key] = record_id
            return self._outcome(entry)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, record_id: str) -> Record:
        """读取一条有效记录；不存在或已删除 -> ``RecordNotFoundError``。"""
        with self._lock:
            entry = self._require_entry(record_id)
            if entry.deleted:
                raise RecordNotFoundError(record_id)
            return self._to_record(entry)

    def get_snapshot(self, record_id: str) -> RecordSnapshot:
        """读取当前快照（删除态也可见），用于检查版本与状态。"""
        with self._lock:
            entry = self._require_entry(record_id)
            return self._to_snapshot(entry, entry.history[-1])

    def history(self, record_id: str) -> tuple[RecordSnapshot, ...]:
        """返回全部历史版本快照（版本号升序），用于审计与复现。"""
        with self._lock:
            entry = self._require_entry(record_id)
            return tuple(self._to_snapshot(entry, rev) for rev in entry.history)

    def list_all_ids_and_liveness(self) -> tuple[tuple[str, bool], ...]:
        """内省辅助：返回 ``(record_id, is_live)``（含已删除记录）。

        主要供测试在并发场景下断言"同一业务键下至多一条有效记录"。
        """
        with self._lock:
            return tuple(
                (rid, not entry.deleted)
                for rid, entry in sorted(self._entries.items(),
                                         key=lambda kv: kv[1].seq)
            )

    def list_records(
        self,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> Page:
        """按 ``(seq, record_id)`` 稳定顺序游标分页，仅含有效记录。

        翻页期间任意插入/更新/删除都不会造成重复或漏读：顺序键在
        记录创建后固定，更新不改变它，新插入的记录只会出现在序列
        末尾（可能出现在最后一页之后，属正常新数据，绝不会插到
        已翻过的页中）。

        游标非法（损坏/伪造/跨实例/版本不支持）抛
        :class:`InvalidCursorError` 或 :class:`StoreMismatchError`；
        游标锚点已因物理清理失效时抛 :class:`ExpiredCursorError`，
        绝不静默退化为从头开始。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive int")
        if limit > MAX_LIMIT:
            raise ValueError(f"limit must be <= {MAX_LIMIT}")

        with self._lock:
            if cursor is None:
                anchor = CursorAnchor(
                    store_id=self._store_id, last_seq=0, last_record_id=""
                )
            else:
                anchor = decode_cursor(cursor, self._store_id, self._secret)
                if anchor.last_seq < self._retained_from_seq:
                    raise ExpiredCursorError(
                        anchor.last_seq, self._retained_from_seq
                    )

            # 按稳定顺序键排序；seq 全局唯一，record_id 仅作确定性兜底
            ordered = sorted(
                self._entries.values(),
                key=lambda e: (e.seq, e.record_id),
            )

            picked: list[_Entry] = []
            for entry in ordered:
                if entry.deleted:
                    continue
                if (entry.seq, entry.record_id) <= (
                    anchor.last_seq,
                    anchor.last_record_id,
                ):
                    continue
                picked.append(entry)
                if len(picked) == limit + 1:
                    break

            has_more = len(picked) == limit + 1
            page_entries = picked[:limit]
            items = tuple(self._to_record(e) for e in page_entries)

            if page_entries and has_more:
                # 仅当确定还有更多数据时才签发续页游标，
                # 避免把客户端引向一个空页
                last = page_entries[-1]
                next_anchor = CursorAnchor(
                    store_id=self._store_id,
                    last_seq=last.seq,
                    last_record_id=last.record_id,
                )
                next_cursor = encode_cursor(next_anchor, self._secret)
            else:
                next_cursor = None

            return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def purge_before(self, seq: int) -> int:
        """物理清理创建序号早于 ``seq`` 的 *已删除* 记录及其历史。

        活跃记录永不被清理（分页与读取不受影响）。清理会推进失效
        水位：锚点早于水位的旧游标之后将得到
        :class:`ExpiredCursorError`。返回被物理移除的记录数。
        """
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("seq must be a non-negative int")
        with self._lock:
            doomed = [
                rid
                for rid, e in self._entries.items()
                if e.deleted and e.seq < seq
            ]
            for rid in doomed:
                del self._entries[rid]
            self._retained_from_seq = max(self._retained_from_seq, seq)
            return len(doomed)
