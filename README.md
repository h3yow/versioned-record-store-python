# versioned-record-store-python

带版本控制的记录存储（内存实现 + FastAPI）。支持：

- 记录的创建、更新、读取，**基于客户端已知版本的乐观并发控制**；
- 按稳定顺序键的**游标分页**，翻页期间插入/删除/更新不重不漏；
- **软删除与恢复**，业务唯一键语义确定，恢复冲突明确拒绝；
- 所有失败都有**可区分的异常类型 / 错误码**，并发与异常下行为可解释、可复现。

## 1. 核心概念

| 概念 | 说明 |
| --- | --- |
| `key` | 业务唯一键。任意时刻，同一个 key 下至多有一条**有效**记录。 |
| `version` | 记录版本，从 `1` 开始；每次 `update`/`delete`/`restore` 严格 `+1`。 |
| `seq` | 记录首次创建时分配的全局单调递增序号，**终生不变**，用作分页顺序键 `(seq, record_id)`。 |
| 有效记录 | 未处于软删除状态的记录；只有有效记录占用业务唯一键、出现在分页结果中。 |

## 2. 版本冲突规则（乐观锁）

所有条件写操作（`update` / `delete` / `restore`）都必须携带客户端已知的
`expected_version`。存储在同一把锁内原子地完成"版本检查 + 状态推进"：

1. 记录不存在 → `RecordNotFoundError`（HTTP `404 NOT_FOUND`）。
2. `expected_version != 当前版本` → `StaleVersionError`
   （HTTP `409 VERSION_CONFLICT`，响应体含 `expected_version` 与
   `current_version`，**调用方据此可区分失败原因并决定重试**）。
3. 版本匹配但记录状态与操作不符（例如更新/删除一条已删除记录、恢复一条
   有效记录）→ `ConcurrentDeleteError`，其 `expected_deleted` /
   `actual_deleted` 字段表明状态判定依据（HTTP `409 NOT_LIVE` /
   `409 NOT_DELETED`）。

并发保证：

- **不丢失更新**：N 个线程以同一旧版本并发写同一条记录，恰好 1 个成功，
  其余全部得到 `StaleVersionError`（见 `test_versioning.py` 中 20 线程用例）。
- **创建唯一键不冲突**：同 key 并发创建，恰好 1 个成功，其余得到
  `DuplicateKeyError`（HTTP `409 DUPLICATE_KEY`）。
- **无部分写入**：任何冲突都在状态被修改*之前*抛出；写操作要么完整生效，
  要么完全不变。失败后 `history()` 中不会出现半截版本。

客户端正确姿势：读取得到 `version` → 携带该版本写入 → 遇到
`VERSION_CONFLICT` 时重新读取最新版本后再决策（合并或重试）。

## 3. 分页与游标的兼容约定

### 3.1 稳定性保证

分页按顺序键 `(seq, record_id)` 升序。`seq` 在记录创建时一次性分配且不再
改变，因此：

- **更新**记录：版本变化但 `seq` 不变 → 不会改变它在任何后续页中的位置；
- **删除**记录（包括已发出游标所锚定的那条）：游标编码的是顺序位置
  `(seq, record_id)`，不依赖记录存活，游标仍然有效，被删记录只是不再出现；
- **插入**记录：永远落在序列尾部，不会插入到已经翻过的页中，因此不会把未读
  记录"顶"出结果。在一次翻页过程开始后才创建的记录，可能出现在最后一页
  之后，属于正常的新数据，而不是漏读。

结论：翻页期间的插入/删除/更新不会对**存量记录**造成重复或漏读；若需要
"某一时刻快照"的严格语义，客户端应在翻页开始前记录时间点并在应用层过滤，
本存储保证的是 keyset 分页的位置稳定性。

### 3.2 游标格式与校验

游标为不透明字符串：`base64url(payload).base64url(hmac_sha256)`，载荷为

```json
{"v": 1, "s": "<store_id>", "seq": 12, "id": "<record_id>"}
```

- `v`：游标格式版本（当前为 `1`）。服务端只接受自己支持的版本；
- `s`：签发它的存储实例 ID；
- 密钥为每个存储实例随机生成（进程内），因此客户端**无法伪造**游标，也
  无法手工构造一个"从头开始"的游标。

非法游标一律**明确拒绝，绝不静默退化为第一页**：

| 情况 | 异常 | HTTP |
| --- | --- | --- |
| 结构损坏 / 非法 base64 / JSON | `InvalidCursorError("malformed…")` | `400 INVALID_CURSOR` |
| 签名不匹配（含篡改、跨实例密钥不同） | `InvalidCursorError("signature mismatch")` | `400 INVALID_CURSOR` |
| 签名有效但 `store_id` 不符（白盒可构造） | `StoreMismatchError` | `400 STORE_MISMATCH` |
| 游标格式版本不支持 | `InvalidCursorError("unsupported cursor version")` | `400 INVALID_CURSOR` |
| 游标锚点已被物理清理（见下） | `ExpiredCursorError`（含 `anchor_seq` / `retained_from_seq`） | `410 CURSOR_EXPIRED` |

### 3.3 失效与清理

`purge_before(seq)` 会物理移除创建序号早于 `seq` 的**已删除**记录（活跃
记录永不清），并推进"保留水位"。之后，锚点 `seq` 低于水位的旧游标会得到
`ExpiredCursorError`，而不是从头开始或静默截断。客户端收到该错误后应放弃
旧游标、重新从第一页开始。

### 3.4 兼容性约定（升级时）

- 新增载荷字段时保持 `v=1` 向后兼容；无法兼容的变更必须提升 `v`，旧游标
  收到明确的 `unsupported cursor version`，而不是乱序或崩溃。
- `next_cursor` 仅在 `has_more=true` 时非空；`has_more=false` 时为
  `null`，客户端不应再继续请求。
- 游标是一次性位置令牌，不应长期持久化、不应跨存储实例复用。

## 4. 软删除与恢复语义

1. `delete(record_id, expected_version)`：
   - 版本推进，记录进入删除态；从 `get` 与分页结果中消失；
   - **释放业务唯一键**：删除后可以用同一个 key 创建新记录；
   - 记录实体与全部版本历史保留（`get_snapshot` / `history` 可读）；
   - 重复删除（已在删除态）→ `ConcurrentDeleteError`（`NOT_LIVE`）。
2. `restore(record_id, expected_version)`：
   - 仅对删除态记录有效；恢复后版本继续 `+1`，重新占用业务唯一键；
   - 若该 key 已被**其他有效记录**占用 → `RestoreConflictError`
     （HTTP `409 RESTORE_CONFLICT`，响应体给出冲突的
     `existing_record_id`），恢复方与占用方状态都不变；
   - 对本就有效的记录恢复 → `ConcurrentDeleteError`（`NOT_DELETED`）。
3. `record_id` 永不复用：即便旧记录已删除，显式以同一 id 创建也会被拒，
   保证版本历史可永久追溯。
4. 并发的删除与恢复在同一临界区内裁决，业务键登记（`key → live record`）
   的变更与记录状态变更原子发生，**不可能产生两个同时有效且同 key 的
   记录**（`test_delete_restore.py` 中 20 轮 × 双线程用例逐轮断言该不变量）。

唯一性的确定性表述：*对任意 key，在任意时刻，有效记录集合中至多一条记录
拥有该 key；该不变量在崩溃边界（异常抛出）与并发边界（多线程）下都成立。*

## 5. HTTP API 摘要

| 方法与路径 | 说明 |
| --- | --- |
| `POST /records` | `{key, data, record_id?}` → 201；重复键 409 `DUPLICATE_KEY` |
| `GET /records?limit=&cursor=` | 游标分页 |
| `GET /records/{id}` | 读取有效记录 |
| `PUT /records/{id}` | `{data, expected_version}`；版本冲突 409 `VERSION_CONFLICT` |
| `POST /records/{id}/delete` | `{expected_version}` 软删除 |
| `POST /records/{id}/restore` | `{expected_version}` 恢复 |

错误响应统一为：

```json
{"error": {"code": "VERSION_CONFLICT",
           "message": "…",
           "detail": {"expected_version": 1, "current_version": 3}}}
```

## 6. 本地验证

要求 Python ≥ 3.14（见 `pyproject.toml`），使用 [uv](https://docs.ast.astral.sh/)：

```bash
# 安装依赖（含 pytest）
uv sync

# 运行全部单测（日志实时打印版本号、游标与判定依据）
uv run pytest

# 仅看汇总
uv run pytest -q

# 按主题运行
uv run pytest tests/test_versioning.py       # 乐观锁 + 并发不丢更新
uv run pytest tests/test_pagination.py       # 翻页稳定性 + 非法/失效游标
uv run pytest tests/test_delete_restore.py   # 删除/恢复冲突 + 并发互斥
uv run pytest tests/test_api.py              # HTTP 错误码端到端
```

测试是确定性的、可重复运行（并发用例使用屏障在同一时刻释放线程，断言
"恰好一个成功 + 其余按原因分类"，不依赖时序运气）。日志示例：

```
线程 08 被拒: expected=1 current=2（StaleVersionError）
判定依据: expected_version=1 与 current_version=2 不一致 -> stale version …
第 1 页 cursor=eyJpZCI6I… -> ids=[…] has_more=True next=eyJzZXF…
轮 07 恢复线程被拒[RestoreConflictError]: cannot restore … key 'round/7' …
```

启动 HTTP 服务手工验证：

```bash
uv run python main.py
# 可选：固定实例 ID 便于复现
VRS_STORE_ID=demo uv run python main.py
```

```bash
curl -s -X POST localhost:8000/records \
  -H 'content-type: application/json' \
  -d '{"key":"k1","data":{"v":1}}'
curl -s 'localhost:8000/records?limit=10'
```

## 7. 代码结构

```
versioned_record_store/
  models.py    # Record / RecordSnapshot / Page / WriteOutcome（不可变值对象）
  errors.py    # 全部可区分异常（携带判定字段）
  cursor.py    # 游标 HMAC 签名、编码与校验
  store.py     # VersionedRecordStore：锁内原子检查+写入、分页、清理
  api.py       # FastAPI 路由与错误码映射
tests/         # 4 个主题、20 个用例（含多线程并发场景）
main.py        # 应用入口
```
