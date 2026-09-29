"""pytest 公共夹具。"""

from __future__ import annotations

import pytest

from versioned_record_store.store import VersionedRecordStore


@pytest.fixture
def store() -> VersionedRecordStore:
    s = VersionedRecordStore()
    try:
        yield s
    finally:
        s.close()
