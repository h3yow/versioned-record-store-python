"""错误类型与可区分的失败原因。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """所有可对外暴露的失败原因。"""

    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    UNIQUE_KEY_CONFLICT = "UNIQUE_KEY_CONFLICT"
    RESTORE_KEY_CONFLICT = "RESTORE_KEY_CONFLICT"
    ALREADY_DELETED = "ALREADY_DELETED"
    DELETED_RECORD = "DELETED_RECORD"
    INVALID_CURSOR = "INVALID_CURSOR"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"
    CURSOR_SCOPE_MISMATCH = "CURSOR_SCOPE_MISMATCH"
    MALFORMED_CURSOR = "MALFORMED_CURSOR"
    STORE_CLOSED = "STORE_CLOSED"


class StoreError(Exception):
    """存储层错误基类，携带可区分的机器可读原因码。"""

    error_code: ErrorCode

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": self.error_code.value,
            "message": self.message,
            "details": self.details,
        }


class NotFoundError(StoreError):
    error_code = ErrorCode.NOT_FOUND


class VersionConflictError(StoreError):
    error_code = ErrorCode.VERSION_CONFLICT


class UniqueKeyConflictError(StoreError):
    error_code = ErrorCode.UNIQUE_KEY_CONFLICT


class RestoreKeyConflictError(StoreError):
    error_code = ErrorCode.RESTORE_KEY_CONFLICT


class AlreadyDeletedError(StoreError):
    error_code = ErrorCode.ALREADY_DELETED


class DeletedRecordError(StoreError):
    error_code = ErrorCode.DELETED_RECORD


class InvalidCursorError(StoreError):
    error_code = ErrorCode.INVALID_CURSOR


class CursorExpiredError(StoreError):
    error_code = ErrorCode.CURSOR_EXPIRED


class CursorScopeMismatchError(StoreError):
    error_code = ErrorCode.CURSOR_SCOPE_MISMATCH


class MalformedCursorError(StoreError):
    error_code = ErrorCode.MALFORMED_CURSOR


class StoreClosedError(StoreError):
    error_code = ErrorCode.STORE_CLOSED
