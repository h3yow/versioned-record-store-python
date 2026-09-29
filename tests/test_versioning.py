"""记录的创建/更新/读取与乐观版本控制，含并发冲突。"""

from __future__ import annotations

import threading

import pytest

from versioned_record_store import (
    DuplicateKeyError,
    RecordNotFoundError,
    StaleVersionError,
    VersionedRecordStore,
)


def test_create_get_and_version_starts_at_one(log):
    store = VersionedRecordStore(store_id="versioning-basic")
    outcome = store.create("user/1", {"name": "alice"})
    log.info("create key=user/1 -> id=%s version=%s seq=%s",
             outcome.record_id, outcome.version, outcome.seq)
    assert outcome.version == 1

    record = store.get(outcome.record_id)
    log.info("get id=%s -> version=%s data=%s",
             record.record_id, record.version, dict(record.data))
    assert record.version == 1
    assert record.data == {"name": "alice"}
    assert record.seq == 1

    with pytest.raises(RecordNotFoundError) as exc:
        store.get("missing-id")
    log.info("判定依据: id=missing-id 不存在 -> %s", exc.value)


def test_duplicate_business_key_is_rejected(log):
    store = VersionedRecordStore(store_id="versioning-dup")
    first = store.create("unique-key", {"n": 1})
    log.info("首次占用 unique-key -> id=%s version=%s",
             first.record_id, first.version)

    with pytest.raises(DuplicateKeyError) as exc:
        store.create("unique-key", {"n": 2})
    log.info("判定依据: 业务键已被有效记录 %s 占用 -> %s",
             exc.value.existing_record_id, exc.value)
    assert exc.value.key == "unique-key"
    assert exc.value.existing_record_id == first.record_id


def test_update_requires_known_version(log):
    store = VersionedRecordStore(store_id="versioning-stale")
    outcome = store.create("k", {"v": 0})

    updated = store.update(outcome.record_id, {"v": 1}, expected_version=1)
    log.info("update expected=1 -> 实际 version=%s", updated.version)
    assert updated.version == 2

    with pytest.raises(StaleVersionError) as exc:
        store.update(outcome.record_id, {"v": 9}, expected_version=1)
    log.info("判定依据: expected_version=%s 与 current_version=%s 不一致 -> %s",
             exc.value.expected_version, exc.value.current_version, exc.value)
    assert exc.value.expected_version == 1
    assert exc.value.current_version == 2

    # 失败的写入不得改变状态（无部分写入）
    assert store.get(outcome.record_id).version == 2
    assert store.get(outcome.record_id).data == {"v": 1}


def test_concurrent_updates_never_lose_a_writer(log):
    """N 个线程同时以相同的旧版本写入：恰好一个成功，其余得到可区分的
    StaleVersionError；随后各线程依次基于最新版本重试，每次更新都不丢。"""
    store = VersionedRecordStore(store_id="versioning-concurrent")
    initial = store.create("counter", {"n": 0})
    rid = initial.record_id

    thread_count = 20
    barrier = threading.Barrier(thread_count)
    successes: list[int] = []
    stale_rejections: list[tuple[int, int]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            outcome = store.update(rid, {"n": idx}, expected_version=1)
            with lock:
                successes.append(idx)
            log.info("线程 %02d 首次写入成功 version=%s", idx, outcome.version)
        except StaleVersionError as exc:
            with lock:
                stale_rejections.append(
                    (exc.expected_version, exc.current_version)
                )
            log.info("线程 %02d 被拒: expected=%s current=%s（%s）",
                     idx, exc.expected_version, exc.current_version, type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("首轮: 成功数=%d 过期拒绝数=%d", len(successes), len(stale_rejections))
    assert len(successes) == 1
    assert len(stale_rejections) == thread_count - 1
    # 所有拒绝都必须能区分原因：期望 1、当前 2
    assert all(ev == 1 and cv == 2 for ev, cv in stale_rejections)

    # 其余线程基于最新版本串行重试：每次版本严格 +1，更新不丢
    for idx in (i for i in range(thread_count) if i not in successes):
        current = store.get(rid)
        outcome = store.update(rid, {"n": idx}, expected_version=current.version)
        log.info("线程 %02d 基于 version=%s 重试成功 -> version=%s",
                 idx, current.version, outcome.version)

    final = store.get(rid)
    log.info("最终 version=%s data=%s", final.version, dict(final.data))
    assert final.version == thread_count + 1
    # 历史版本连续完整，可复现审计
    history = store.history(rid)
    versions = [h.version for h in history]
    log.info("历史版本号序列=%s", versions)
    assert versions == list(range(1, thread_count + 2))


def test_concurrent_creates_same_key_only_one_wins(log):
    store = VersionedRecordStore(store_id="versioning-concurrent-create")
    thread_count = 12
    barrier = threading.Barrier(thread_count)
    winners: list[str] = []
    dup = 0
    lock = threading.Lock()

    def worker(idx: int) -> None:
        nonlocal dup
        barrier.wait()
        try:
            outcome = store.create("race-key", {"by": idx})
            with lock:
                winners.append(outcome.record_id)
        except DuplicateKeyError as exc:
            with lock:
                dup += 1
            log.info("线程 %02d 唯一键冲突，占用者=%s", idx,
                     exc.existing_record_id)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("create 竞争: 唯一成功者=%s 重复键拒绝数=%d", winners, dup)
    assert len(winners) == 1
    assert dup == thread_count - 1
