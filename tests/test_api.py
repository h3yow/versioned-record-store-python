"""HTTP 端到端：失败原因映射为稳定 error.code；游标分页端到端不重不漏。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from versioned_record_store import VersionedRecordStore
from versioned_record_store.api import create_router, error_handlers_app
from fastapi import FastAPI


@pytest.fixture
def client() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    store = VersionedRecordStore(store_id="api-test")
    app.include_router(create_router(store))
    error_handlers_app(app)
    return TestClient(app)


def _create(client: TestClient, key: str, data=None, record_id=None):
    body = {"key": key, "data": data or {}}
    if record_id is not None:
        body["record_id"] = record_id
    return client.post("/records", json=body)


def test_error_codes_are_distinguishable(client, log):
    r = _create(client, "k1", {"v": 1})
    assert r.status_code == 201
    rid = r.json()["record_id"]
    log.info("create 201 id=%s version=1", rid)

    # 重复键 -> 409 DUPLICATE_KEY
    r = _create(client, "k1")
    log.info("重复键 -> %s body=%s", r.status_code, r.json())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_KEY"

    # 版本过期 -> 409 VERSION_CONFLICT（与重复键可区分）
    r = client.put(f"/records/{rid}",
                   json={"data": {"v": 2}, "expected_version": 99})
    log.info("版本过期 -> %s body=%s", r.status_code, r.json())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"
    assert r.json()["error"]["detail"]["current_version"] == 1

    # 不存在 -> 404 NOT_FOUND
    r = client.get("/records/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"

    # 成功更新后版本推进
    r = client.put(f"/records/{rid}",
                   json={"data": {"v": 2}, "expected_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2


def test_delete_restore_conflict_codes(client, log):
    r = _create(client, "k1", {"who": "old"})
    rid = r.json()["record_id"]

    r = client.post(f"/records/{rid}/delete", json={"expected_version": 1})
    assert r.status_code == 200 and r.json()["deleted"] is True

    # 重复删除 -> 409 NOT_LIVE
    r = client.post(f"/records/{rid}/delete", json={"expected_version": 2})
    log.info("重复删除 -> %s code=%s", r.status_code,
             r.json()["error"]["code"])
    assert r.json()["error"]["code"] == "NOT_LIVE"

    # 别人占用了业务键
    other = _create(client, "k1", {"who": "new"}).json()["record_id"]

    # 恢复冲突 -> 409 RESTORE_CONFLICT（原因可区分）
    r = client.post(f"/records/{rid}/restore", json={"expected_version": 2})
    log.info("恢复冲突 -> %s code=%s", r.status_code,
             r.json()["error"]["code"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESTORE_CONFLICT"
    assert r.json()["error"]["detail"]["existing_record_id"] == other

    # 新记录删除后，旧记录恢复成功，版本继续前进
    client.post(f"/records/{other}/delete", json={"expected_version": 1})
    r = client.post(f"/records/{rid}/restore", json={"expected_version": 2})
    assert r.status_code == 200 and r.json()["version"] == 3


def test_pagination_end_to_end_with_mutations(client, log):
    for i in range(5):
        assert _create(client, f"k{i}", {"i": i}).status_code == 201

    seen: list[str] = []
    cursor = None
    deleted_midway = None
    for page_no in range(10):
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get("/records", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        log.info("第 %d 页 cursor=%s -> ids=%s has_more=%s",
                 page_no, (cursor[:16] + "…") if cursor else None,
                 [it["record_id"] for it in body["items"]], body["has_more"])
        ids = [it["record_id"] for it in body["items"]]
        assert not (set(ids) & set(seen)), "翻页出现重复"
        seen.extend(ids)

        if page_no == 0 and body["items"]:
            # 翻页间隙删除一条尚未读到的记录
            deleted_midway = client.get("/records", params={"limit": 50}).json()
            victim = [x for x in deleted_midway["items"]
                      if x["record_id"] not in seen][-1]
            dr = client.post(f"/records/{victim['record_id']}/delete",
                             json={"expected_version": victim["version"]})
            log.info("翻页间隙删除 id=%s -> %s", victim["record_id"],
                     dr.status_code)

        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        cursor = body["next_cursor"]

    log.info("端到端收集到 %d 条，无重复", len(seen))
    assert len(seen) == len(set(seen))


def test_invalid_and_cross_store_cursors_rejected(client, log):
    _create(client, "k1")
    _create(client, "k2")

    r = client.get("/records", params={"limit": 1, "cursor": "not-a-cursor"})
    log.info("非法游标 -> %s code=%s", r.status_code,
             r.json()["error"]["code"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CURSOR"

    # 另一个实例签发的游标（签名必然不符）
    other_app = FastAPI()
    other_store = VersionedRecordStore(store_id="other-instance")
    other_app.include_router(create_router(other_store))
    other = TestClient(error_handlers_app(other_app))
    other.post("/records", json={"key": "x", "data": {}})
    other.post("/records", json={"key": "y", "data": {}})
    foreign = other.get("/records", params={"limit": 1}).json()["next_cursor"]
    r = client.get("/records", params={"limit": 1, "cursor": foreign})
    log.info("跨实例游标 -> %s code=%s", r.status_code,
             r.json()["error"]["code"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] in {"INVALID_CURSOR", "STORE_MISMATCH"}
