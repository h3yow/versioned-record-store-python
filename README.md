# versioned-record-store-python

带版本控制的内存记录存储，支持并发编辑、稳定游标分页、软删除与恢复。
核心实现不依赖 Web 框架（`versioned_record_store/store.py`），
FastAPI 只是其外的一层 HTTP 适配（`versioned_record_store/api.py`）。

## 运行

```bash
uv sync
uv run python main.py        # 启动 HTTP 服务，默认 127.0.0.1:8000
```

## 本地验证

```bash
uv sync                     # 安装含 pytest 的开发依赖
uv run pytest               # 全部单测，实时打印 INFO 日志
uv run pytest -q            # 安静模式
uv run pytest tests/test_versioning.py    # 只跑版本/并发/删除恢复
uv run pytest tests/test_pagination.py   # 只跑分页与游标
uv run pytest tests/test_api.py          # 只跑 HTTP 端到端
```

测试可重复运行、无外部依赖、无网络、不监听端口（HTTP 用 TestClient 进程内调用）。
日志中会打印每次判定的 **版本号**、**游标** 与 **判定依据**（pytest 已开启
`log_cli`，INFO 级别实时输出）。

## 数据模型

| 字段 | 含义 |
| --- | --- |
| `id` | 记录 id，存储内唯一且永不复用（`rec_000001` 起） |
| `key` | 业务唯一键，**仅在有效（未删除）记录之间**保持唯一 |
| `data` | 业务数据（JSON 对象） |
| `version` | 整数版本号：创建为 `1`；每次成功的更新/删除/恢复 `+1` |
| `deleted` | 是否处于软删除态 |
| `created_at` / `updated_at` / `deleted_at` | 存储内单调逻辑时钟 |

## 版本冲突规则（乐观并发控制）

- 创建返回 `version=1`。
- 更新、删除、恢复都必须携带 `expected_version`（客户端最后读到的版本号）。
- 存储在单一锁内串行化所有写入，并比较 `expected_version` 与当前版本：
  - 一致：写入生效，版本号 `+1`，原子返回新记录；
  - 不一致：拒绝写入，返回 `VERSION_CONFLICT`，错误体 `details` 带回
    `expected_version` 与 `current_version`，客户端据此重新拉取并重试。
- 因此两个同时到达、基于同一版本的写入 **只有一个成功**，另一个得到
  `VERSION_CONFLICT`，不会丢失更新；所有失败的写入不产生任何副作用，
  不会出现“写了一半却返回成功”。
- `expected_version = -1`（`ANY_VERSION`）可显式跳过版本检查，需要显式传入，
  默认不推荐在并发写路径使用。
- 记录处于软删除态时，更新返回 `DELETED_RECORD`，需先恢复。

失败原因（HTTP 状态码）：

| 错误码 | 状态码 | 触发场景 |
| --- | --- | --- |
| `NOT_FOUND` | 404 | id / 有效 key 不存在 |
| `DELETED_RECORD` | 410 | 记录已软删除（默认读取或更新） |
| `VERSION_CONFLICT` | 409 | expected_version 与当前版本不一致 |
| `UNIQUE_KEY_CONFLICT` | 409 | 创建时 key 已被有效记录占用 |
| `RESTORE_KEY_CONFLICT` | 409 | 恢复时 key 已被另一条有效记录占用 |
| `ALREADY_DELETED` | 409 | 重复删除，或对有效记录执行恢复 |
| `INVALID_CURSOR` | 400 | 游标签名有效但位置在快照中不存在 |
| `MALFORMED_CURSOR` | 400 | 游标为空、结构损坏、签名失败（含跨存储实例） |
| `CURSOR_SCOPE_MISMATCH` | 400 | active 与 all 两种作用域游标混用 |
| `CURSOR_EXPIRED` | 410 | 游标绑定的快照已被淘汰 |
| `STORE_CLOSED` | 503 | 存储关闭后的调用 |

错误响应体统一为：

```json
{"error": "VERSION_CONFLICT", "message": "...", "details": {"expected_version": 1, "current_version": 2}}
```

## 分页与游标的兼容约定

- 排序固定为 `(key ASC, id ASC)`，id 永不复用，因此顺序稳定、全序。
- `GET /records?page_size=20&cursor=...&include_deleted=false`
  - `page_size` 范围 `1..100`；
  - 响应：`items`、`next_cursor`（末页为 `null`）、`snapshot_id`、`has_more`。
- **快照语义**：每次成功写入都会产生一个新的不可变快照。不带游标时，列表
  绑定当前最新快照；游标中编码了 `snapshot_id` 与上一页末尾位置，后续翻页
  始终从同一快照读取。于是翻页期间无论发生插入、删除还是更新：
  - 已在遍历中的结果集不变，**不会重复，也不会漏读**；
  - 变更从下一次“从头开始”的新遍历（新快照）起才可见。
- **游标是不透明字符串**：base64url(JSON) 内含版本位 `v=1`、快照 id、
  末尾 `(key, id)`、作用域（`active` / `all`）以及 HMAC-SHA256 签名。
  客户端不得解析或自行构造，只应原样回传。
- 非法/失效游标一律被**明确拒绝**，绝不退化为从头开始、也不静默忽略：
  - 空串、损坏、签名不通过（含来自其他存储实例）→ `MALFORMED_CURSOR`；
  - 作用域不一致（`include_deleted` 取值不同）→ `CURSOR_SCOPE_MISMATCH`；
  - 快照已淘汰 → `CURSOR_EXPIRED`（客户端应放弃遍历并从头重新建立游标）；
  - 签名有效但位置在快照中找不到 → `INVALID_CURSOR`。
- 兼容性：`v` 为游标格式版本位。当前仅支持 `v=1`；未来升级格式时，旧版本
  游标会被明确判为 `MALFORMED_CURSOR`（而非产生错误结果）。同一存储实例的
  HMAC 密钥在实例存活期内固定；重启进程后旧游标自然失效。
- 快照有界保留（默认最近 64 个，构造参数 `max_snapshots`），防止无限增长；
  被淘汰快照对应的旧游标返回 `CURSOR_EXPIRED`。

## 软删除与恢复语义

- `DELETE /records/{id}?expected_version=N` 为软删除：记录数据、id、历史
  key 均保留，状态变为 `deleted=true`，版本号 `+1`；可通过
  `include_deleted=true` 读到，默认读取返回 `DELETED_RECORD`。
- **唯一性语义确定**：唯一键只约束有效记录。软删除即释放 key，之后允许用同
  一 key 创建新记录；因此同一 key 在历史上可能存在多条记录，但任意时刻至多
  有一条有效记录占用它。
- `POST /records/{id}/restore`（带 `expected_version`）把软删除记录恢复为
  有效：
  - 若该 key 当前没有其他有效记录：恢复成功，版本号 `+1`；
  - 若已被另一条有效记录占用（典型：删除后又用同 key 新建）：拒绝并返回
    `RESTORE_KEY_CONFLICT`，`details.conflicting_id` 指明现存记录。需要业务
    侧显式决策（例如先删除/再处理现存记录）后才能恢复，系统不会自动合并。
- 并发删除与恢复在同一把锁内串行化：两者携带的 `expected_version` 只有先到
  的一方能匹配成功，后到方得到 `VERSION_CONFLICT` / `ALREADY_DELETED`，
  因此不会出现两个同时有效的记录，也不会出现唯一键被两条有效记录占用。

## HTTP 接口一览

```
POST   /records                       {"key": "...", "data": {...}}
GET    /records?page_size=&cursor=&include_deleted=
GET    /records/{id}?include_deleted=
PUT    /records/{id}                  {"expected_version": 1, "data": {...}}
DELETE /records/{id}?expected_version=1
POST   /records/{id}/restore          {"expected_version": 2}
```

## 项目结构

```
versioned_record_store/
  errors.py    # 错误码与异常（可区分的失败原因）
  models.py    # Record / Page / ANY_VERSION
  cursor.py    # 游标编解码与 HMAC 签名
  store.py     # 线程安全存储、快照、乐观版本控制、软删除/恢复
  api.py       # FastAPI 路由与错误码->HTTP 状态映射
tests/
  test_versioning.py   # 版本冲突、并发无丢失更新、删除恢复冲突
  test_pagination.py   # 翻页期间增删改、非法/过期/跨作用域游标
  test_api.py          # HTTP 状态码与错误体
```
