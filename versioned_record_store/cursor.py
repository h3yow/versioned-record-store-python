"""游标编码 / 解码。

游标为不透明字符串，结构是 base64url(JSON)：

    {"v": 1, "s": <快照id>, "k": <末条key>, "i": <末条id>,
     "p": <作用域>, "h": <HMAC-SHA256 签名>}

本模块只负责：编码、结构解析、签名校验、作用域比对。
快照是否仍保留（游标是否过期）等语义由 store 层判定。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final

from .errors import CursorScopeMismatchError, MalformedCursorError

CURSOR_VERSION: Final = 1


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(token: str) -> bytes:
    try:
        pad = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + pad)
    except (ValueError, TypeError) as exc:
        raise MalformedCursorError("游标不是合法的 base64url 字符串") from exc


def _sign(payload: dict, secret: bytes) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(secret, body, hashlib.sha256).digest()
    return _b64e(mac)


def encode_cursor(
    *,
    snapshot_id: int,
    last_key: str,
    last_id: str,
    scope: str,
    secret: bytes,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "s": snapshot_id,
        "k": last_key,
        "i": last_id,
        "p": scope,
    }
    payload["h"] = _sign(payload, secret)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64e(raw)


def decode_cursor(token: str, *, scope: str, secret: bytes) -> tuple[int, str, str]:
    """返回 (snapshot_id, last_key, last_id)。

    结构/签名非法 -> MalformedCursorError；作用域不一致 ->
    CursorScopeMismatchError。
    """
    if not isinstance(token, str) or not token:
        raise MalformedCursorError("游标为空或类型错误")

    raw = _b64d(token)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedCursorError("游标内容不是合法 JSON") from exc

    if not isinstance(payload, dict):
        raise MalformedCursorError("游标载荷结构错误")

    sig = payload.pop("h", None)
    if not isinstance(sig, str):
        raise MalformedCursorError("游标缺少签名")
    if payload.get("v") != CURSOR_VERSION:
        raise MalformedCursorError("游标版本不被支持")

    expected = _sign(payload, secret)
    if not hmac.compare_digest(sig, expected):
        raise MalformedCursorError("游标签名校验失败（可能被篡改或来自其他存储实例）")

    snapshot_id = payload.get("s")
    last_key = payload.get("k")
    last_id = payload.get("i")
    token_scope = payload.get("p")
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool):
        raise MalformedCursorError("游标中的快照 id 非法")
    if not isinstance(last_key, str) or not isinstance(last_id, str):
        raise MalformedCursorError("游标中的位置字段非法")
    if not isinstance(token_scope, str):
        raise MalformedCursorError("游标中的作用域非法")

    if token_scope != scope:
        raise CursorScopeMismatchError(
            "游标与当前列表作用域不匹配",
            details={"cursor_scope": token_scope, "expected_scope": scope},
        )

    return snapshot_id, last_key, last_id
