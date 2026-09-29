"""可按原因区分的失败类型。

所有失败都带有结构化字段（当前版本、期望版本、游标等），
便于调用方判定，也便于测试日志打印"判定依据"。
"""

from __future__ import annotations


class StoreError(Exception):
    """所有存储层异常的基类。"""


class ConflictError(StoreError):
    """写入冲突的基类（乐观锁失败、唯一键冲突等）。"""


class RecordNotFoundError(StoreError):
    """记录不存在。"""

    def __init__(self, record_id: str):
        self.record_id = record_id
        super().__init__(f"record not found: {record_id!r}")


class DuplicateKeyError(ConflictError):
    """业务唯一键已被另一条有效记录占用。"""

    def __init__(self, key: str, existing_record_id: str):
        self.key = key
        self.existing_record_id = existing_record_id
        super().__init__(
            f"duplicate business key: {key!r} "
            f"is held by live record {existing_record_id!r}"
        )


class StaleVersionError(ConflictError):
    """条件写入基于的版本已过期（乐观锁冲突），拒绝以防丢失更新。"""

    def __init__(self, record_id: str, expected_version: int, current_version: int):
        self.record_id = record_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"stale version for record {record_id!r}: "
            f"expected {expected_version}, current is {current_version}"
        )


class RestoreConflictError(ConflictError):
    """恢复一条软删除记录时，其业务键已被现存有效记录占用。"""

    def __init__(self, record_id: str, key: str, existing_record_id: str):
        self.record_id = record_id
        self.key = key
        self.existing_record_id = existing_record_id
        super().__init__(
            f"cannot restore record {record_id!r}: key {key!r} "
            f"is now held by live record {existing_record_id!r}"
        )


class ConcurrentDeleteError(ConflictError):
    """记录已经被并发删除/恢复（当前状态与条件不符）。"""

    def __init__(self, record_id: str, expected_deleted: bool, actual_deleted: bool):
        self.record_id = record_id
        self.expected_deleted = expected_deleted
        self.actual_deleted = actual_deleted
        state = "deleted" if actual_deleted else "live"
        wanted = "deleted" if expected_deleted else "live"
        super().__init__(
            f"record {record_id!r} is {state} but operation required {wanted}"
        )


class CursorError(StoreError):
    """游标类失败的基类。"""


class InvalidCursorError(CursorError):
    """游标非法：编码损坏、签名不匹配、版本不支持或属于其他存储实例。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"invalid cursor: {reason}")


class ExpiredCursorError(CursorError):
    """游标曾合法，但其锚点记录已被物理清理（无法再保证无漏读）。"""

    def __init__(self, anchor_seq: int, retained_from_seq: int):
        self.anchor_seq = anchor_seq
        self.retained_from_seq = retained_from_seq
        super().__init__(
            f"cursor expired: anchor seq {anchor_seq} < retained seq "
            f"{retained_from_seq}"
        )


class StoreMismatchError(CursorError):
    """游标由另一个存储实例签发。"""

    def __init__(self, expected_store_id: str, cursor_store_id: str):
        self.expected_store_id = expected_store_id
        self.cursor_store_id = cursor_store_id
        super().__init__(
            f"cursor belongs to store {cursor_store_id!r}, "
            f"not {expected_store_id!r}"
        )
