"""软删除与恢复：唯一键语义确定、恢复冲突被拒、并发删除/恢复互斥。"""

from __future__ import annotations

import threading

import pytest

from versioned_record_store import (
    ConcurrentDeleteError,
    RecordNotFoundError,
    RestoreConflictError,
    StaleVersionError,
    VersionedRecordStore,
)


def test_delete_is_soft_and_key_becomes_reusable(log):
    store = VersionedRecordStore(store_id="del-basic")
    rec = store.create("k1", {"v": 1})
    outcome = store.delete(rec.record_id, expected_version=1)
    log.info("软删除 id=%s -> version=%s deleted=%s",
             outcome.record_id, outcome.version, outcome.deleted)
    assert outcome.version == 2 and outcome.deleted is True

    with pytest.raises(RecordNotFoundError):
        store.get(rec.record_id)
    log.info("判定依据: 有效视图读不到已删除记录")

    # 历史仍完整保留，可审计/可复现
    snap = store.get_snapshot(rec.record_id)
    log.info("快照仍可读: version=%s deleted=%s seq=%s",
             snap.version, snap.deleted, snap.seq)
    history = store.history(rec.record_id)
    assert [h.version for h in history] == [1, 2]
    assert history[-1].deleted is True

    # 删除后业务键可被新记录重新占用
    reborn = store.create("k1", {"v": 2})
    log.info("业务键复用: 新记录 id=%s version=%s seq=%s",
             reborn.record_id, reborn.version, reborn.seq)
    assert reborn.record_id != rec.record_id


def test_restore_succeeds_when_key_free(log):
    store = VersionedRecordStore(store_id="del-restore-ok")
    rec = store.create("k1", {})
    store.delete(rec.record_id, expected_version=1)
    restored = store.restore(rec.record_id, expected_version=2)
    log.info("恢复成功: version 2 -> %s, deleted=%s",
             restored.version, restored.deleted)
    assert restored.version == 3 and restored.deleted is False
    assert store.get(rec.record_id).version == 3

    # 恢复后业务键重新唯一
    with pytest.raises(Exception) as exc:
        store.create("k1", {})
    log.info("判定依据: 恢复后唯一键重新生效 -> %s", exc.value)


def test_restore_conflicts_with_existing_live_record(log):
    store = VersionedRecordStore(store_id="del-restore-conflict")
    old = store.create("k1", {"who": "old"})
    store.delete(old.record_id, expected_version=1)
    new = store.create("k1", {"who": "new"})  # 业务键被新记录占用
    log.info("旧记录 id=%s 已删除(version=2); 新占用者 id=%s version=%s",
             old.record_id, new.record_id, new.version)

    with pytest.raises(RestoreConflictError) as exc:
        store.restore(old.record_id, expected_version=2)
    log.info("判定依据: 恢复会产生两个同键有效记录 -> %s", exc.value)
    assert exc.value.existing_record_id == new.record_id

    # 冲突后双方状态都不变（无部分写入）
    assert store.get(new.record_id).record_id == new.record_id
    assert store.get_snapshot(old.record_id).deleted is True


def test_double_delete_and_restore_live_are_distinguishable(log):
    store = VersionedRecordStore(store_id="del-state-mismatch")
    rec = store.create("k1", {})
    store.delete(rec.record_id, expected_version=1)  # version -> 2

    with pytest.raises(ConcurrentDeleteError) as exc:
        store.delete(rec.record_id, expected_version=2)
    log.info("判定依据[重复删除]: wanted=%s actual=%s -> %s",
             "live" if not exc.value.expected_deleted else "deleted",
             "deleted" if exc.value.actual_deleted else "live", exc.value)

    store.restore(rec.record_id, expected_version=2)  # -> 3
    with pytest.raises(ConcurrentDeleteError) as exc:
        store.restore(rec.record_id, expected_version=3)
    log.info("判定依据[恢复有效记录]: wanted=deleted actual=live -> %s",
             exc.value)


def test_concurrent_delete_and_restore_never_both_live(log):
    """20 轮、每轮 2 个线程分别以同一已知版本执行删除与恢复：
    只有一个操作成功；失败原因必须可区分（过期/状态不符/键冲突），
    且任意时刻业务键下至多一条有效记录。"""
    store = VersionedRecordStore(store_id="del-race")
    rounds = 20
    outcomes = {"delete_ok": 0, "restore_ok": 0,
                "stale": 0, "state": 0, "key_conflict": 0}
    lock = threading.Lock()

    def classify(exc: Exception) -> str:
        if isinstance(exc, StaleVersionError):
            return "stale"
        if isinstance(exc, RestoreConflictError):
            return "key_conflict"
        if isinstance(exc, ConcurrentDeleteError):
            return "state"
        raise AssertionError(f"未预期的异常类型: {type(exc).__name__}: {exc}")

    for rnd in range(rounds):
        rec = store.create(f"round/{rnd}", {"round": rnd})
        # 先删到 deleted 态，使删除/恢复双方都有合理的初始尝试空间：
        # 双方都从 version=2(已删) 出发，一个再删(状态不符)，一个恢复
        store.delete(rec.record_id, expected_version=1)
        known_version = store.get_snapshot(rec.record_id).version
        winner = {}
        barrier = threading.Barrier(2)

        def do_delete() -> None:
            barrier.wait()
            try:
                store.delete(rec.record_id, expected_version=known_version)
                with lock:
                    outcomes["delete_ok"] += 1
                    winner["delete"] = True
                log.info("轮 %02d 删除线程成功（id=%s v=%s）",
                         rnd, rec.record_id, known_version)
            except Exception as exc:  # noqa: BLE001 - 下面按类型归类
                with lock:
                    outcomes[classify(exc)] += 1
                log.info("轮 %02d 删除线程被拒[%s]: %s",
                         rnd, type(exc).__name__, exc)

        def do_restore() -> None:
            barrier.wait()
            try:
                store.restore(rec.record_id, expected_version=known_version)
                with lock:
                    outcomes["restore_ok"] += 1
                    winner["restore"] = True
                log.info("轮 %02d 恢复线程成功（id=%s v=%s）",
                         rnd, rec.record_id, known_version)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    outcomes[classify(exc)] += 1
                log.info("轮 %02d 恢复线程被拒[%s]: %s",
                         rnd, type(exc).__name__, exc)

        t1 = threading.Thread(target=do_delete)
        t2 = threading.Thread(target=do_restore)
        t1.start(); t2.start(); t1.join(); t2.join()

        # 不变量：一轮中至多一个成功
        assert len(winner) == 1, winner
        # 不变量：业务键下绝不出现两个同时有效的记录
        live = [e for e in store.list_all_ids_and_liveness()
                if e[1] and store.get_snapshot(e[0]).key == f"round/{rnd}"]
        assert len(live) <= 1

    log.info("并发删除/恢复汇总: %s", outcomes)
    # 恢复要么成功，要么得到明确的冲突/过期/状态原因；删除线程在
    # 已删状态下总是得到 ConcurrentDeleteError（或恢复先成功后的过期错误）
    assert outcomes["delete_ok"] + outcomes["restore_ok"] == rounds


def test_concurrent_delete_updates_same_record_only_one_succeeds(log):
    """两个线程同时删除同一有效记录（相同 expected_version）：
    一个成功，另一个得到 StaleVersionError。"""
    store = VersionedRecordStore(store_id="del-race-same-op")
    rec = store.create("k1", {})
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            store.delete(rec.record_id, expected_version=1)
            with lock:
                results.append("ok")
        except StaleVersionError as exc:
            with lock:
                results.append("stale")
            log.info("判定依据: expected=%s current=%s -> %s",
                     exc.expected_version, exc.current_version, exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("并发同删结果=%s", sorted(results))
    assert sorted(results) == ["ok", "stale"]
    assert store.get_snapshot(rec.record_id).version == 2
