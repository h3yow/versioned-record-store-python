"""核心数据模型（不可变值对象）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 哨兵：调用方表示“不检查版本”
ANY_VERSION = -1


@dataclass(frozen=True, slots=True)
class Record:
    """一条记录。

    version 为单调递增的整数版本号：
      创建 -> 1；每次更新/删除/恢复 成功 -> +1。
    deleted=True 表示软删除状态，数据与 key 仍保留。
    """

    id: str
    key: str
    data: dict[str, Any]
    version: int
    deleted: bool = False
    created_at: int = 0
    updated_at: int = 0
    deleted_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "data": dict(self.data),
            "version": self.version,
            "deleted": self.deleted,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


@dataclass(frozen=True, slots=True)
class RecordUpdate:
    """更新请求体；data 为 None 表示不修改数据。"""

    expected_version: int = ANY_VERSION
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Page:
    """一页结果，绑定生成它的快照。"""

    items: tuple[Record, ...]
    next_cursor: str | None
    snapshot_id: int
    has_more: bool
