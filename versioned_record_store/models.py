"""数据模型（不可变值对象）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    """某条记录在某个版本下的不可变快照。"""

    record_id: str
    key: str
    version: int
    data: Mapping[str, Any]
    seq: int
    deleted: bool
    store_id: str


@dataclass(frozen=True, slots=True)
class Record:
    """对外可见的（未删除）记录视图。"""

    record_id: str
    key: str
    version: int
    data: Mapping[str, Any]
    seq: int


@dataclass(frozen=True, slots=True)
class Page:
    """游标分页的一页结果。

    顺序键为 ``(seq, record_id)``：``seq`` 是记录首次创建时分配的
    单调递增序号，终生不变，因此翻页期间的更新/删除/插入不会导致
    重复或漏读。
    """

    items: tuple[Record, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """一次成功写入的结果（创建/更新/删除/恢复通用）。"""

    record_id: str
    key: str
    version: int
    seq: int
    deleted: bool
