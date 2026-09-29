"""游标的编码、签名与校验。

游标是自描述的不透明令牌：``base64url(payload_json).base64url(hmac)``，
其中 HMAC-SHA256 的密钥是每个存储实例随机生成的，因此游标不可伪造、
不可跨实例复用，也无法被客户端手工构造出"从头开始"的效果。

其锚点是翻页顺序键 ``(seq, record_id)``，语义为"从该锚点 *之后*
继续"。``seq`` 在记录创建时分配且终生不变，因此翻页期间发生的
插入、更新、删除都不会改变已发出游标的含义。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .errors import InvalidCursorError, StoreMismatchError

CURSOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class CursorAnchor:
    """游标解码后的锚点：表示"从该顺序键之后继续"。"""

    store_id: str
    last_seq: int
    last_record_id: str
    version: int = CURSOR_VERSION


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    # 补齐标准 base64 所需的 padding
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encode_cursor(anchor: CursorAnchor, secret: bytes) -> str:
    """把锚点编码为签名后的不透明游标字符串。"""
    payload = {
        "v": anchor.version,
        "s": anchor.store_id,
        "seq": anchor.last_seq,
        "id": anchor.last_record_id,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(sig)}"


def decode_cursor(token: str, store_id: str, secret: bytes) -> CursorAnchor:
    """校验并解码游标。

    非法/损坏/签名错误 -> ``InvalidCursorError``；
    属于其他存储实例 -> ``StoreMismatchError``；
    版本号不支持 -> ``InvalidCursorError``。
    """
    if not isinstance(token, str) or "." not in token:
        raise InvalidCursorError("malformed token structure")

    body_text, _, sig_text = token.rpartition(".")
    if not body_text or not sig_text:
        raise InvalidCursorError("malformed token structure")

    try:
        body = _b64decode(body_text)
        sig = _b64decode(sig_text)
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError("token is not valid base64url") from exc

    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise InvalidCursorError("signature mismatch")

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidCursorError("payload is not an object")

    version = payload.get("v")
    if version != CURSOR_VERSION:
        raise InvalidCursorError(f"unsupported cursor version: {version!r}")

    cursor_store_id = payload.get("s")
    if not isinstance(cursor_store_id, str):
        raise InvalidCursorError("missing store id in cursor")
    if cursor_store_id != store_id:
        raise StoreMismatchError(store_id, cursor_store_id)

    last_seq = payload.get("seq")
    last_record_id = payload.get("id")
    # bool 是 int 的子类，需显式排除
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
        raise InvalidCursorError(f"invalid anchor seq: {last_seq!r}")
    if not isinstance(last_record_id, str) or not last_record_id:
        raise InvalidCursorError("invalid anchor record id")

    return CursorAnchor(
        store_id=cursor_store_id,
        last_seq=last_seq,
        last_record_id=last_record_id,
        version=version,
    )
