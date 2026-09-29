"""稳定顺序的游标分页：翻页期间数据变化不重不漏；非法/失效游标被拒绝。"""

from __future__ import annotations

import pytest

from versioned_record_store import (
    ExpiredCursorError,
    InvalidCursorError,
    StoreMismatchError,
    VersionedRecordStore,
)
from versioned_record_store.cursor import (
    CURSOR_VERSION,
    CursorAnchor,
    encode_cursor,
)


def _walk(store: VersionedRecordStore, limit: int, log, start: str | None = None,
          mutator=None):
    """逐页走完全部记录，记录每一页的游标与判定依据。"""
    cursor = start
    pages = []
    seen: list[str] = []
    step = 0
    while True:
        page = store.list_records(limit=limit, cursor=cursor)
        pages.append(page)
        ids = [r.record_id for r in page.items]
        log.info("第 %d 页 cursor=%s -> ids=%s has_more=%s next=%s",
                 step, (cursor[:16] + "…") if cursor else None,
                 ids, page.has_more,
                 (page.next_cursor[:16] + "…") if page.next_cursor else None)
        seen.extend(ids)
        if mutator is not None:
            mutator(step)
            mutator = None  # 仅在第一页之后施加一次变更
        if page.next_cursor is None:
            assert page.has_more is False
            break
        cursor = page.next_cursor
        step += 1
    return pages, seen


def test_pagination_is_stable_and_complete(log):
    store = VersionedRecordStore(store_id="page-basic")
    created = [store.create(f"k{i:02d}", {"i": i}) for i in range(7)]

    pages, seen = _walk(store, limit=3, log=log)
    log.info("共 %d 页, 收集到 %d 条 id", len(pages), len(seen))
    assert len(seen) == 7
    assert len(set(seen)) == 7  # 无重复
    # 顺序严格按创建 seq 升序
    assert seen == [o.record_id for o in created]
    # has_more 与下一页游标一致
    assert [p.has_more for p in pages] == [True, True, False]


def test_pagination_during_mutations_no_dup_no_miss(log):
    """翻到第一页之后：更新已读记录、删除已读/未读记录、尾部插入新记录，
    继续翻页不得重复或漏读任何仍然有效的存量记录。"""
    store = VersionedRecordStore(store_id="page-mutate")
    recs = {f"r{i}": store.create(f"k{i}", {"i": i})
            for i in range(1, 10)}  # seq 1..9

    def mutate(step: int) -> None:
        # 已读页内的更新：不改顺序，仅推进版本号
        u = store.update(recs["r2"].record_id, {"i": 2, "u": True},
                         expected_version=1)
        log.info("变更: 更新 r2 -> version=%s（顺序键 seq=2 不变）", u.version)
        # 删除一条已读记录（r3）和一条未读记录（r5）
        d3 = store.delete(recs["r3"].record_id, expected_version=1)
        d5 = store.delete(recs["r5"].record_id, expected_version=1)
        log.info("变更: 删除 r3(seq=3,已读) -> version=%s; "
                 "删除 r5(seq=5,未读) -> version=%s", d3.version, d5.version)
        # 翻页期间的新插入落在序列尾部
        n10 = store.create("k10", {"i": 10})
        n11 = store.create("k11", {"i": 11})
        log.info("变更: 尾部插入 seq=%s 与 seq=%s", n10.seq, n11.seq)

    pages, seen = _walk(store, limit=3, log=log, mutator=mutate)

    log.info("翻页结束，共收集 id 序列=%s", seen)
    assert len(seen) == len(set(seen)), "出现重复记录"

    # r1..r9 中除未读时被删的 r5 外，全部恰好出现一次；
    # 已读过的 r3 即使随后被删也不会在后续页重复/错位
    expected_stable = {recs[f"r{i}"].record_id for i in range(1, 10) if i != 5}
    assert set(seen) >= expected_stable, "存量有效记录发生漏读"

    # 新插入只可能出现在尾部，且至多一次
    assert seen.count(seen[-1]) == 1
    tail_seqs = [store.get_snapshot(rid).seq for rid in seen[-2:]]
    log.info("尾部两条 seq=%s（新数据位于序列尾部，不会插进已翻过的页）",
             tail_seqs)
    assert tail_seqs == sorted(tail_seqs)

    # r2 的更新对后续读取可见且顺序仍在原位
    assert store.get(recs["r2"].record_id).version == 2


def test_anchor_record_deleted_does_not_break_cursor(log):
    """游标锚点记录在翻页间隙被删除，游标仍然有效（锚点只是顺序位置）。"""
    store = VersionedRecordStore(store_id="page-anchor-delete")
    recs = [store.create(f"k{i}", {}) for i in range(6)]
    page1 = store.list_records(limit=3)
    assert [r.record_id for r in page1.items] == [r.record_id for r in recs[:3]]
    cursor = page1.next_cursor
    log.info("游标锚点 seq=3: %s", cursor[:24] + "…")

    store.delete(recs[2].record_id, expected_version=1)  # 锚点记录被删
    log.info("判定依据: 锚点只编码 (seq, record_id)，不依赖记录存活")
    page2 = store.list_records(limit=3, cursor=cursor)
    assert [r.record_id for r in page2.items] == [r.record_id for r in recs[3:]]
    assert page2.next_cursor is None


def test_invalid_cursors_are_explicitly_rejected(log):
    store = VersionedRecordStore(store_id="page-invalid")
    other = VersionedRecordStore(store_id="page-other")
    store.create("k1", {})
    store.create("k2", {})  # 至少两页，才会产生续页游标
    other.create("k1", {})
    other.create("k2", {})
    other_cursor = other.list_records(limit=1).next_cursor
    log.info("跨实例游标: %s", other_cursor[:24] + "…")

    cases = [
        ("garbage", store, "完全非法字符串"),
        ("a.b", store, "结构像游标但无法解码"),
        (other_cursor, store, "由其他存储实例签发"),
    ]
    for token, target, desc in cases:
        with pytest.raises((InvalidCursorError, StoreMismatchError)) as exc:
            target.list_records(limit=1, cursor=token)
        log.info("判定依据[%s]: %s -> %s",
                 desc, type(exc.value).__name__, exc.value)

    # 截断/篡改合法游标 -> 签名不匹配（绝不退化从头开始）
    page = store.list_records(limit=1)
    good = page.next_cursor
    log.info("合法游标: %s", good)
    with pytest.raises(InvalidCursorError) as exc:
        store.list_records(limit=1, cursor=good[:-2] + "aa")
    log.info("判定依据[篡改]: %s -> %s",
             type(exc.value).__name__, exc.value)

    # 不支持的游标版本号（白盒：用本实例 secret 签一个 v=2 游标）
    future = encode_cursor(
        CursorAnchor(store_id=store.store_id, last_seq=1,
                     last_record_id="x", version=CURSOR_VERSION + 1),
        store._secret,
    )
    with pytest.raises(InvalidCursorError) as exc:
        store.list_records(limit=1, cursor=future)
    log.info("判定依据[版本不支持]: %s -> %s",
             type(exc.value).__name__, exc.value)

    # 签名合法但 store_id 不符（白盒构造）-> StoreMismatchError
    foreign = encode_cursor(
        CursorAnchor(store_id="some-other-store", last_seq=1,
                     last_record_id="x", version=CURSOR_VERSION),
        store._secret,
    )
    with pytest.raises(StoreMismatchError) as exc:
        store.list_records(limit=1, cursor=foreign)
    log.info("判定依据[store_id 不符]: %s -> %s",
             type(exc.value).__name__, exc.value)


def test_expired_cursor_after_purge_is_rejected_not_reset(log):
    store = VersionedRecordStore(store_id="page-expired")
    first = store.create("early", {})   # seq=1，稍后删除
    for i in range(2, 5):
        store.create(f"k{i}", {})
    page = store.list_records(limit=2)
    old_cursor = page.next_cursor
    log.info("失效前游标（锚点 seq=2）: %s", old_cursor[:24] + "…")

    store.delete(first.record_id, expected_version=1)
    purged = store.purge_before(100)
    log.info("物理清理 %d 条记录，失效水位=100", purged)

    with pytest.raises(ExpiredCursorError) as exc:
        store.list_records(limit=2, cursor=old_cursor)
    log.info("判定依据: anchor_seq=%s < retained_from_seq=%s -> %s",
             exc.value.anchor_seq, exc.value.retained_from_seq, exc.value)
    assert exc.value.anchor_seq < exc.value.retained_from_seq
