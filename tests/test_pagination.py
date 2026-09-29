"""游标分页：稳定顺序、翻页期间数据变化、非法与失效游标。"""

from __future__ import annotations

import logging

import pytest

from versioned_record_store.errors import (
    CursorExpiredError,
    CursorScopeMismatchError,
    InvalidCursorError,
    MalformedCursorError,
)

logger = logging.getLogger("vrs.test_pagination")


def _walk_all(store, **kwargs):
    collected = []
    cursor = None
    seen_snapshots = []
    while True:
        page = store.list_page(cursor=cursor, **kwargs)
        seen_snapshots.append(page.snapshot_id)
        collected.extend(page.items)
        logger.info(
            "翻页：snapshot_id=%s 本页=%s 游标=%s",
            page.snapshot_id,
            [(r.key, r.version) for r in page.items],
            (page.next_cursor[:18] + "...") if page.next_cursor else None,
        )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return collected, seen_snapshots


def test_stable_order_and_no_duplicates_or_gaps(store):
    for i in range(7):
        store.create(key=f"k{i:02d}", data={"i": i})
    collected, snapshots = _walk_all(store, page_size=3)
    keys = [r.key for r in collected]
    logger.info(
        "全量遍历 key 序列=%s，绑定快照=%s，判定：顺序=(key,id) 稳定且无重复无漏读",
        keys, snapshots,
    )
    assert keys == [f"k{i:02d}" for i in range(7)]
    assert len(keys) == len(set(keys))
    assert set(snapshots) == {snapshots[0]}


def test_insertions_during_paging_do_not_duplicate_or_lose(store):
    for i in range(5):
        store.create(key=f"k{i:02d}", data={"i": i})
    page1 = store.list_page(page_size=2)
    first_keys = [r.key for r in page1.items]
    logger.info(
        "第1页 snapshot_id=%s keys=%s next_cursor=%s",
        page1.snapshot_id, first_keys, page1.next_cursor[:18] + "...",
    )

    # 翻页期间：在序列开头、中间、结尾各插入
    store.create(key="a00", data={})
    store.create(key="k02b", data={})
    store.create(key="z99", data={})

    page2 = store.list_page(page_size=2, cursor=page1.next_cursor)
    page3 = store.list_page(page_size=2, cursor=page2.next_cursor)
    # 旧快照总共 5 条，第 3 页后只剩 1 条，游标为 None
    assert page3.next_cursor is None
    old_view = page1.items + page2.items + page3.items
    keys = [r.key for r in old_view]
    logger.info(
        "插入后继续沿旧游标翻页：序列=%s，判定：仍是快照创建时的 5 条，"
        "不重复（无 a00/k02b/z99）也不漏读",
        keys,
    )
    assert keys == [f"k{i:02d}" for i in range(5)]
    assert page1.snapshot_id == page3.snapshot_id

    fresh, _ = _walk_all(store, page_size=10)
    fresh_keys = [r.key for r in fresh]
    logger.info("重新从头遍历得到新快照视图=%s，判定：新插入的 3 条在新遍历中可见", fresh_keys)
    assert set(fresh_keys) == {f"k{i:02d}" for i in range(5)} | {"a00", "k02b", "z99"}


def test_deletes_during_paging_keep_old_view_consistent(store):
    records = [store.create(key=f"k{i:02d}", data={}) for i in range(4)]
    page1 = store.list_page(page_size=2)
    store.delete(records[2].id, expected_version=1)  # 删除尚未读到的 k02
    store.delete(records[0].id, expected_version=1)  # 删除已读过的 k00

    page2 = store.list_page(page_size=2, cursor=page1.next_cursor)
    keys = [r.key for r in page1.items + page2.items]
    logger.info(
        "翻页期间删除 k00/k02 后旧视图序列=%s，判定：旧游标视图仍完整、不漏读，"
        "下一轮新遍历才反映删除",
        keys,
    )
    assert keys == ["k00", "k01", "k02", "k03"]
    fresh, _ = _walk_all(store, page_size=10)
    assert [r.key for r in fresh] == ["k01", "k03"]


def test_updates_during_paging_pin_snapshot_view(store):
    record = store.create(key="k00", data={"v": 1})
    for i in range(1, 4):
        store.create(key=f"k{i:02d}", data={})
    page1 = store.list_page(page_size=2)
    store.update(record.id, expected_version=1, data={"v": 99})

    page2 = store.list_page(page_size=10, cursor=page1.next_cursor)
    old_first = page1.items[0]
    logger.info(
        "翻页期间更新 k00：旧快照中 version=%s data=%s；判定：整轮遍历绑定同一快照，"
        "同一遍历内记录版本保持一致、可复现",
        old_first.version, old_first.data,
    )
    assert old_first.version == 1
    assert store.get(record.id).version == 2
    assert page2.snapshot_id == page1.snapshot_id


def test_malformed_cursors_are_rejected(store):
    for bad in ["", "not-a-cursor", "eyJmb28iOjF9", "a" * 40]:
        with pytest.raises(MalformedCursorError):
            store.list_page(cursor=bad)
    logger.info("非法游标字符串集合=%s 全部判定为 MALFORMED_CURSOR，绝不退化从头开始", 4)


def test_tampered_cursor_is_rejected(store):
    store.create(key="k00", data={})
    store.create(key="k01", data={})
    page = store.list_page(page_size=1)
    token = page.next_cursor
    assert token is not None
    # 翻转 token 最后一个字符以伪造位置
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(MalformedCursorError) as exc:
        store.list_page(page_size=1, cursor=tampered)
    logger.info(
        "篡改游标 原=%s 伪=%s -> 原因=%s，判定：签名失败必须明确拒绝",
        token[:18] + "...", tampered[:18] + "...", exc.value.error_code.value,
    )


def test_cursor_scope_mismatch_is_rejected(store):
    for i in range(3):
        store.create(key=f"k{i:02d}", data={})
    page_active = store.list_page(page_size=1, include_deleted=False)
    with pytest.raises(CursorScopeMismatchError) as exc:
        store.list_page(page_size=1, cursor=page_active.next_cursor, include_deleted=True)
    logger.info(
        "active 作用域游标拿到 all 列表使用 -> 原因=%s details=%s，"
        "判定：不同 include_deleted 作用域游标不得混用",
        exc.value.error_code.value, exc.value.details,
    )


def test_cursor_from_other_store_is_rejected():
    from versioned_record_store.store import VersionedRecordStore

    store_a = VersionedRecordStore()
    store_b = VersionedRecordStore()
    store_a.create(key="k00", data={})
    store_a.create(key="k01", data={})
    foreign = store_a.list_page(page_size=1).next_cursor
    with pytest.raises((MalformedCursorError, CursorExpiredError)) as exc:
        store_b.list_page(page_size=1, cursor=foreign)
    logger.info(
        "跨存储实例游标判定=%s（HMAC 不同或快照不存在，绝不退化为从头遍历）",
        exc.value.error_code.value,
    )
    store_a.close()
    store_b.close()


def test_expired_cursor_eviction_path():
    from versioned_record_store.store import VersionedRecordStore

    s = VersionedRecordStore(max_snapshots=3)
    for i in range(4):
        s.create(key=f"k{i:02d}", data={})
    old = s.list_page(page_size=1)  # 绑定最新快照
    old_cursor = old.next_cursor
    # 再制造足够多的变更快照，把旧快照淘汰
    for i in range(4, 9):
        s.create(key=f"k{i:02d}", data={})
    with pytest.raises(CursorExpiredError) as exc:
        s.list_page(page_size=1, cursor=old_cursor)
    logger.info(
        "旧游标 snapshot_id=%s 在快照淘汰后使用 -> 原因=%s，判定：失效游标明确拒绝，"
        "不静默从头开始",
        old.snapshot_id, exc.value.error_code.value,
    )
    s.close()


def test_cursor_position_missing_is_invalid(store):
    # 手工构造一个签名正确但位置不存在的游标
    from versioned_record_store import cursor as codec

    secret = store._secret
    token = codec.encode_cursor(
        snapshot_id=0, last_key="ghost", last_id="rec_999999",
        scope="active", secret=secret,
    )
    with pytest.raises(InvalidCursorError) as exc:
        store.list_page(page_size=1, cursor=token)
    logger.info(
        "签名有效但位置不存在的游标 -> 原因=%s details=%s，判定：INVALID_CURSOR",
        exc.value.error_code.value, exc.value.details,
    )


def test_page_size_validation(store):
    with pytest.raises(ValueError):
        store.list_page(page_size=0)
    with pytest.raises(ValueError):
        store.list_page(page_size=101)
    logger.info("page_size=0 / 101 判定为参数错误")
