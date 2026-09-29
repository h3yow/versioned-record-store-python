"""HTTP 层：状态码、错误原因区分、游标分页与删除恢复端到端。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from versioned_record_store.api import create_app
from versioned_record_store.store import VersionedRecordStore

logger = logging.getLogger("vrs.test_api")


@pytest.fixture
def client():
    store = VersionedRecordStore()
    app = create_app(store)
    with TestClient(app) as c:
        yield c
    store.close()


def test_create_and_get(client):
    resp = client.post("/records", json={"key": "doc:1", "data": {"a": 1}})
    assert resp.status_code == 201
    body = resp.json()
    logger.info("HTTP 创建成功 id=%s version=%s", body["id"], body["version"])
    assert body["version"] == 1

    got = client.get(f"/records/{body['id']}").json()
    assert got["data"] == {"a": 1}


def test_version_conflict_status_and_body(client):
    created = client.post("/records", json={"key": "doc:1"}).json()
    client.put(f"/records/{created['id']}", json={"expected_version": 1, "data": {"v": 2}})
    resp = client.put(
        f"/records/{created['id']}", json={"expected_version": 1, "data": {"v": 3}}
    )
    logger.info(
        "过期版本写入 HTTP 状态=%s 响应=%s，判定：409 + VERSION_CONFLICT + current_version",
        resp.status_code, resp.json(),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "VERSION_CONFLICT"
    assert resp.json()["details"]["current_version"] == 2


def test_unique_key_conflict_is_distinct(client):
    client.post("/records", json={"key": "doc:1"})
    resp = client.post("/records", json={"key": "doc:1"})
    logger.info("唯一键冲突 HTTP=%s body=%s", resp.status_code, resp.json())
    assert resp.status_code == 409
    assert resp.json()["error"] == "UNIQUE_KEY_CONFLICT"


def test_not_found_and_deleted_statuses(client):
    resp = client.get("/records/rec_000001")
    assert resp.status_code == 404
    assert resp.json()["error"] == "NOT_FOUND"

    created = client.post("/records", json={"key": "doc:1"}).json()
    deleted = client.delete(f"/records/{created['id']}?expected_version=1")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    gone = client.get(f"/records/{created['id']}")
    logger.info("删除后默认读取 HTTP=%s body=%s", gone.status_code, gone.json())
    assert gone.status_code == 410
    assert gone.json()["error"] == "DELETED_RECORD"


def test_restore_conflict_end_to_end(client):
    old = client.post("/records", json={"key": "doc:1"}).json()
    old_deleted = client.delete(f"/records/{old['id']}?expected_version=1").json()
    new = client.post("/records", json={"key": "doc:1"}).json()

    resp = client.post(
        f"/records/{old['id']}/restore",
        json={"expected_version": old_deleted["version"]},
    )
    logger.info(
        "恢复冲突 HTTP=%s body=%s，判定：409 + RESTORE_KEY_CONFLICT + conflicting_id=%s",
        resp.status_code, resp.json(), new["id"],
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "RESTORE_KEY_CONFLICT"
    assert resp.json()["details"]["conflicting_id"] == new["id"]


def test_pagination_cursor_flow_and_invalid_cursor(client):
    for i in range(5):
        client.post("/records", json={"key": f"k{i:02d}"})

    page1 = client.get("/records?page_size=2")
    assert page1.status_code == 200
    body1 = page1.json()
    cursor = body1["next_cursor"]
    logger.info(
        "HTTP 第1页 snapshot_id=%s keys=%s next_cursor=%s",
        body1["snapshot_id"], [r["key"] for r in body1["items"]], cursor[:18] + "...",
    )

    page2 = client.get(f"/records?page_size=2&cursor={cursor}")
    assert [r["key"] for r in page2.json()["items"]] == ["k02", "k03"]

    bad = client.get("/records?cursor=garbage")
    logger.info("非法游标 HTTP=%s body=%s", bad.status_code, bad.json())
    assert bad.status_code == 400
    assert bad.json()["error"] == "MALFORMED_CURSOR"


def test_scope_mismatch_cursor_rejected(client):
    for i in range(3):
        client.post("/records", json={"key": f"k{i:02d}"})
    cursor = client.get("/records?page_size=1").json()["next_cursor"]
    resp = client.get(f"/records?page_size=1&include_deleted=true&cursor={cursor}")
    logger.info("作用域混用 HTTP=%s body=%s", resp.status_code, resp.json())
    assert resp.status_code == 400
    assert resp.json()["error"] == "CURSOR_SCOPE_MISMATCH"
