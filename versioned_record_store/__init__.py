"""带版本控制的记录存储（versioned record store）。"""

from .errors import (
    ConflictError,
    DuplicateKeyError,
    InvalidCursorError,
    RecordNotFoundError,
    StaleVersionError,
    StoreMismatchError,
    ExpiredCursorError,
    RestoreConflictError,
    ConcurrentDeleteError,
)
from .models import Record, RecordSnapshot, Page, WriteOutcome
from .store import VersionedRecordStore

__all__ = [
    "VersionedRecordStore",
    "Record",
    "RecordSnapshot",
    "Page",
    "WriteOutcome",
    "ConflictError",
    "DuplicateKeyError",
    "RecordNotFoundError",
    "StaleVersionError",
    "StoreMismatchError",
    "InvalidCursorError",
    "ExpiredCursorError",
    "RestoreConflictError",
    "ConcurrentDeleteError",
]
