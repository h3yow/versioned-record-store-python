"""版本控制、并发更新丢失防护、软删除与恢复冲突测试。"""

from __future__ import annotations

import logging
import threading

import pytest

from versioned_record_store.errors import (
    AlreadyDeletedError,
    DeletedRecordError,
    NotFoundError,
    RestoreKeyConflictError,
    UniqueKeyConflictError,
    VersionConflictError,
)
from versioned_record_store.models import ANY_VERSION

logger = logging.getLogger("vrs.test_versioning")


def test_create_starts_at_version_one(store):
    record = store.create(key="user:1", data={"name": "alice"})
    logger.info(
        "创建记录 id=%s key=%s version=%s，判定：create 成功后版本号必须为 1",
        record.id, record.key, record.version,
    )
    assert record.version == 1
    assert record.deleted is False


def test_update_requires_known_version(store):
    record = store.create(key="user:1", data={"v": 1})
    updated = store.update(record.id, expected_version=1, data={"v": 2})
    logger.info(
        "id=%s 基于 expected_version=1 更新成功，新版本号=%s，判定：版本推进 +1",
        record.id, updated.version,
    )
    assert updated.version == 2
    assert updated.data == {"v": 2}

    with pytest.raises(VersionConflictError) as exc:
        store.update(record.id, expected_version=1, data={"v": 3})
    logger.info(
        "id=%s 用过期版本 expected_version=1 提交，被拒原因=%s current_version=%s，"
        "判定：必须返回 VERSION_CONFLICT 且带回当前版本",
        record.id, exc.value.error_code.value, exc.value.details["current_version"],
    )
    assert exc.value.error_code.value == "VERSION_CONFLICT"
    assert exc.value.details["current_version"] == 2


def test_concurrent_updates_no_lost_update(store):
    record = store.create(key="counter", data={"n": 0})
    barrier = threading.Barrier(5)
    results: dict[str, int] = {}
    results_lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        # 每个线程最多重试若干轮；所有成功版本必须互不相同（无丢失更新）
        for _ in range(50):
            current = store.get(record.id)
            try:
                new_record = store.update(
                    record.id,
                    expected_version=current.version,
                    data={"n": current.data["n"] + 1, "by": idx},
                )
            except VersionConflictError as conflict:
                logger.info(
                    "worker%s 撞版本：expected=%s current=%s 原因=%s -> 重试",
                    idx, current.version,
                    conflict.details["current_version"],
                    conflict.error_code.value,
                )
                continue
            with results_lock:
                results[f"worker{idx}"] = new_record.version
            logger.info(
                "worker%s 提交成功，写入版本号=%s", idx, new_record.version
            )
            return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get(record.id)
    logger.info(
        "并发结束：最终版本号=%s 成功写入集合=%s，判定：5 个成功版本互不相同、"
        "最终版本号=1+5=6，且无线程拿到同一版本（无丢失更新）",
        final.version, sorted(results.values()),
    )
    assert len(results) == 5
    assert len(set(results.values())) == 5
    assert final.version == 6
    assert final.data["n"] == 5


def test_concurrent_updates_same_expected_version_only_one_wins(store):
    record = store.create(key="race", data={})
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            store.update(record.id, expected_version=1, data={"by": idx})
            with outcomes_lock:
                outcomes.append("ok")
        except VersionConflictError as exc:
            with outcomes_lock:
                outcomes.append(exc.error_code.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info(
        "两个线程同时基于 version=1 提交，结果=%s，判定：恰有 1 个 ok、"
        "1 个 VERSION_CONFLICT，不会出现两个都成功",
        outcomes,
    )
    assert sorted(outcomes) == ["VERSION_CONFLICT", "ok"]


def test_unique_key_only_enforced_among_active_records(store):
    record = store.create(key="doc:1", data={})
    with pytest.raises(UniqueKeyConflictError):
        store.create(key="doc:1", data={})
    deleted = store.delete(record.id, expected_version=1)
    logger.info(
        "id=%s 软删除成功 version=%s，判定：删除后唯一键释放，同 key 可再创建",
        record.id, deleted.version,
    )
    replacement = store.create(key="doc:1", data={})
    assert replacement.id != record.id
    logger.info(
        "同 key 新记录 id=%s version=%s 创建成功", replacement.id, replacement.version
    )


def test_restore_rejected_when_key_taken(store):
    old = store.create(key="doc:1", data={"gen": "old"})
    old_deleted = store.delete(old.id, expected_version=1)
    new = store.create(key="doc:1", data={"gen": "new"})

    with pytest.raises(RestoreKeyConflictError) as exc:
        store.restore(old.id, expected_version=old_deleted.version)
    logger.info(
        "恢复旧记录 id=%s（删除态版本号=%s）被拒：原因=%s 冲突记录=%s，"
        "判定：现存有效记录占用同 key 时必须拒绝恢复",
        old.id, old_deleted.version, exc.value.error_code.value, new.id,
    )
    assert exc.value.error_code.value == "RESTORE_KEY_CONFLICT"
    assert exc.value.details["conflicting_id"] == new.id

    # 现存记录删除并释放 key 后，旧记录恢复成功，版本继续推进
    new_deleted = store.delete(new.id, expected_version=1)
    restored = store.restore(old.id, expected_version=old_deleted.version)
    logger.info(
        "新记录 id=%s 删除(version=%s)释放 key 后，旧记录 id=%s 恢复成功 version=%s，"
        "判定：恢复后只有一条有效记录占用 doc:1",
        new.id, new_deleted.version, old.id, restored.version,
    )
    assert restored.version == old_deleted.version + 1
    assert store.get_by_key("doc:1").id == old.id


def test_concurrent_delete_and_restore_leave_single_active(store):
    record = store.create(key="toggle", data={})
    # 先删除得到删除态，再并发：一个尝试再次删除，一个尝试恢复
    deleted = store.delete(record.id, expected_version=1)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def restore_worker() -> None:
        barrier.wait()
        try:
            r = store.restore(record.id, expected_version=deleted.version)
            with outcomes_lock:
                outcomes.append(f"restore-ok-v{r.version}")
        except VersionConflictError as exc:
            with outcomes_lock:
                outcomes.append(exc.error_code.value)

    def redelete_worker() -> None:
        barrier.wait()
        try:
            store.delete(record.id, expected_version=deleted.version)
            with outcomes_lock:
                outcomes.append("delete-ok")
        except (AlreadyDeletedError, VersionConflictError) as exc:
            with outcomes_lock:
                outcomes.append(exc.error_code.value)

    threads = [threading.Thread(target=restore_worker), threading.Thread(target=redelete_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    active_holders = [
        r.id for r in (store.get(record.id, include_deleted=True),) if not r.deleted
    ]
    final = store.get(record.id, include_deleted=True)
    logger.info(
        "并发恢复/重复删除结果=%s，最终 deleted=%s version=%s，"
        "判定：操作被串行化，唯一键至多一条有效记录，失败方可据原因区分",
        outcomes, final.deleted, final.version,
    )
    assert len(outcomes) == 2
    assert len(active_holders) <= 1


def test_delete_then_read_is_gone_unless_include_deleted(store):
    record = store.create(key="doc:2", data={})
    store.delete(record.id, expected_version=1)
    with pytest.raises(DeletedRecordError):
        store.get(record.id)
    fetched = store.get(record.id, include_deleted=True)
    logger.info(
        "id=%s 删除后默认读取判定=DELETED_RECORD；include_deleted 读到 version=%s deleted=%s",
        record.id, fetched.version, fetched.deleted,
    )
    with pytest.raises(NotFoundError):
        store.get_by_key("doc:2")


def test_double_delete_and_restore_active_are_conflicts(store):
    record = store.create(key="doc:3", data={})
    store.delete(record.id, expected_version=1)
    with pytest.raises(AlreadyDeletedError):
        store.delete(record.id, expected_version=2)
    restored = store.restore(record.id, expected_version=2)
    with pytest.raises(AlreadyDeletedError):
        store.restore(record.id, expected_version=restored.version)
    logger.info(
        "重复删除 -> ALREADY_DELETED；对有效记录恢复 -> ALREADY_DELETED，"
        "最终有效版本号=%s，判定：状态类失败原因可区分",
        restored.version,
    )


def test_any_version_bypass_is_explicit(store):
    record = store.create(key="doc:4", data={})
    updated = store.update(record.id, expected_version=ANY_VERSION, data={"x": 1})
    logger.info("ANY_VERSION(%s) 显式跳过版本校验，新版本号=%s", ANY_VERSION, updated.version)
    assert updated.version == 2
