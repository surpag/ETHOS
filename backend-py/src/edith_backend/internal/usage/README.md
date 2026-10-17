# usage 模块（用量记录与汇总）

## 第一部分：模块名称与简介

**模块名**：`usage`（用量记录与汇总），源码目录 `edith_backend/internal/usage/`。

**一句话定位**：记录每次 Agent 运行（AgentRun）的 Token 用量，并为会话提供服务器计算的用量汇总（`Summary`），供其他模块展示。

**对应 Go 版源码路径**：`EDITH/backend-v2/internal/usage/`（`module.go` / `reader.go` / `recorder.go` / `store.go` / `types.go`），本模块是其在 Python 版（FastAPI + aiosqlite + Pydantic v2）中的一一对应实现。

**文件清单**：

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块对外导出：`Module`、`Reader`、`Recorder`、`Run`、`Summary`、`Tokens`、`new_module` |
| `module.py` | 组装入口：`Module` 能力容器与 `new_module(db)` 工厂（建表 + 装配 Recorder/Reader） |
| `types.py` | 类型定义：`Run`（运行身份）、`Tokens`（单次运行用量）、`Summary`（会话汇总，Pydantic 模型） |
| `store.py` | SQLite 数据访问层：`usage_runs` 表结构、增改查与汇总聚合 SQL、异常定义 |
| `recorder.py` | 记录器：`start` / `finish` / `fail` 三个写入口，被 agentrun 调用 |
| `reader.py` | 读取器：`status` / `session_summary` 两个读入口，被 conversation 调用 |

## 第二部分：架构设计

### ① 分层

本模块**不包含 HTTP 层**（无路由、无 `http.py`），是纯内部库模块，按代码实际结构分三层：

| 层级 | 文件 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | `Run`（`dataclass`）、`Tokens`（`dataclass`）、`Summary`（Pydantic v2 `BaseModel`，输出 camelCase） |
| 数据访问层（仓储） | `store.py` | `Store` 封装全部 SQL：建表、`start`/`finish`/`fail` 写入、`status`/`session_summary` 查询 |
| 服务层 | `recorder.py`、`reader.py` | `Recorder`（写）、`Reader`（读），只依赖 `Store`，不含 SQL |
| 组装入口 | `module.py` | `Module` 容器 + `new_module(db)` 工厂，负责建表并把 `Store` 注入 `Recorder`/`Reader` |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `store.py:26-27`、`recorder.py:13-14`、`reader.py:13-14`、`module.py:18-20` | `Recorder(store)` / `Reader(store)` / `Module(recorder, reader)` 均以构造器接收依赖；组合根 `main.py:61-63` 负责实际注入 |
| 工厂方法 | `module.py:23` `new_module(db)` | 接收 `aiosqlite.Connection`，建表后返回装配好的 `Module`；与 Go 版 `usage.New` 对应 |
| 仓储模式（Repository） | `store.py:25` `Store` | 所有 SQL 集中在此，服务层不感知数据库细节；异常 `RunAlreadyExists`/`RunNotFound` 也在仓储层定义 |
| 数据类 / Pydantic 模型 | `types.py` | 内部流转用 `dataclass`（`Run`、`Tokens`），对外展示用 `BaseModel`（`Summary`，`alias_generator=to_camel` 输出 camelCase JSON） |

未发现策略、状态机、适配器等模式（状态只是 `usage_runs.status` 列上的常量 `running`/`completed`/`failed`，由 SQL `WHERE status = ?` 保证状态迁移，无显式状态机对象）。

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Module` | `module.py:15` | 能力容器，仅持有 `self.recorder` / `self.reader` 两个属性，无额外逻辑 |
| `new_module(db)` | `module.py:23` | `async def new_module(db: aiosqlite.Connection) -> Module`；校验 db 非空（否则 `ValueError("usage database is required")`）、创建 `Store`、`await store.create_schema()`、装配并返回 `Module` |
| `Store` | `store.py:25` | `def __init__(self, db: aiosqlite.Connection)`；SQLite 访问器，持有连接 `self._db` |
| `Store.create_schema()` | `store.py:29` | 建 `usage_runs` 表与 `usage_runs_session_index` 索引（见第四部分①） |
| `Store.start(run)` | `store.py:52` | `INSERT ... ON CONFLICT(request_id) DO NOTHING`，rowcount≠1 抛 `RunAlreadyExists`；必填字段缺失抛 `ValueError` |
| `Store.finish(run, tokens)` | `store.py:66` | `UPDATE ... WHERE request_id = ? AND status = 'running'`，rowcount≠1 抛 `RunNotFound`；按 `cached_prompt_tokens` 是否为空写 `cache_reported`，按 `tokens.reported` 写 `usage_reported` |
| `Store.fail(request_id)` | `store.py:91` | `UPDATE ... SET status='failed' WHERE request_id = ? AND status = 'running'`，幂等 |
| `Store.status(user_id, request_id)` | `store.py:98` | 查单行 `status`，无行抛 `RunNotFound` |
| `Store.session_summary(user_id, session_id)` | `store.py:109` | 按 `status='completed'` 聚合（COUNT/SUM），返回 `Summary` |
| `Recorder.start / finish / fail` | `recorder.py:16-27` | 写入口薄封装：`finish` 写入后返回新的会话汇总（`await store.finish(...)` 再 `session_summary(...)`） |
| `Reader.status` | `reader.py:16` | `async def status(user_id, request_id) -> str`，转发 `store.status` |
| `Reader.session_summary` | `reader.py:20` | `async def session_summary(user_id, session_id) -> Summary`，转发 `store.session_summary` |
| `Run` | `types.py:14` | `@dataclass`：`request_id` / `user_id` / `session_id` / `model_id`，一次 Agent 运行的稳定身份 |
| `Tokens` | `types.py:24` | `@dataclass`：`prompt_tokens` / `cached_prompt_tokens`（默认 None = 未知）/ `completion_tokens` / `total_tokens` / `reported`（False = 本地估算，非供应商上报） |
| `Summary` | `types.py:34` | Pydantic v2 `BaseModel`（`to_camel` 别名，`populate_by_name=True`）：`total_tokens`、`completion_tokens` 必填；`cached_prompt_tokens`、`uncached_prompt_tokens`、`cache_hit_rate` 可空；覆盖度 `total_runs` / `cache_reported_runs` / `estimated_runs` |
| `RunAlreadyExists` / `RunNotFound` | `store.py:17-22` | 仓储层业务异常（继承 `Exception`），`recorder.py` 的 `__all__` 中重新导出 |

## 第三部分：与其他模块的联系

### ① 依赖关系

**内部模块依赖**：无。本模块不 import 任何其他 `edith_backend.internal.*` 模块，仅被外部注入一个数据库连接。

**外部依赖**：

| 依赖 | 用途 |
| --- | --- |
| `aiosqlite` | `module.py:8`、`store.py:8` —— 异步 SQLite 连接类型与 SQL 执行（连接由组合根 `main.py` 的 `open_database()` 打开后传入） |
| `pydantic`（含 `pydantic.alias_generators.to_camel`） | `types.py:10-11` —— `Summary` 模型定义与 camelCase 序列化 |
| `dataclasses` | `types.py:8` —— `Run` / `Tokens` 定义 |

**被谁依赖（组合根装配，`main.py:61-63`）**：

- `usage = await new_usage_module(db)` —— 模块创建入口；
- `conversations = new_conversation_module(session_store, usage.reader)` —— 把 `Reader` 注入 conversation；
- `agent_runs = Service(Dependencies(..., usage=usage.recorder, ...))`（`main.py:84-98`）—— 把 `Recorder` 注入 agentrun。

### ② 数据流向

以一次典型业务流转为例（浏览器发起 Agent 运行 → 会话查看）：

1. 用户经 HTTP 入口 `POST /internal/gateway/messages:stream`（`webadapter/adapter.py:45`）→ `gateway.Service.run` → `agentrun.Service.run`；
2. `Service.run` 构造 `Run` 并调用 `recorder.start(run)`（`agentrun/service.py:89-96`），`Store.start` 插入 `usage_runs` 一行（`status='running'`）；若 `request_id` 重复，抛 `RunAlreadyExists`，`Service.run` 捕获后返回 `Error("request_conflict", ...)`；
3. 异步任务 `_run` 执行 `chat(...)`（`agentrun/execution.py`），用 `_UsageAccumulator` 累加用量：OpenAI 流式协议的 usage 只出现在**末尾 choices 为空的收尾块**里（代码必须先捕获再跳过空 choices），经 `tokens_from_usage` 转为 `Tokens`；摘要压缩那次模型调用的用量同样计入本次 run；供应商确实没返回 usage 时退化为本地估算并标 `reported=False`；
4. 执行完成后 `recorder.finish(run, tokens)`（`agentrun/service.py:168`）：`Store.finish` 将该行更新为 `status='completed'` 并写入 token 字段，随后 `Store.session_summary` 重算会话汇总并返回；`run.completed` 事件通过 `_summary_to_session_usage` 把 `Summary` 转为 `SessionUsage` 随事件推给前端（`agentrun/service.py:177`）；
5. 执行被取消或异常时，`recorder.fail(request_id)`（`agentrun/service.py:181,191`）把行标记为 `status='failed'`，**失败运行不计入会话汇总**；
6. 用户查看会话历史时，`GET /internal/conversations/{session_id}`（`conversation/http.py:30`）→ `History.get` 调用 `reader.session_summary(user_id, session_id)`（`conversation/history.py:50`），聚合该会话所有 `completed` 行的用量，作为 `ConversationResponse.usage` 返回前端展示。

### ③ 接口调用

**HTTP 路由**：本模块**不暴露任何 HTTP 路由**（无 `http.py`）。用量数据经由以下两个调用方路由间接输出：

| 路由 | 所属模块 | 与 usage 的关系 |
| --- | --- | --- |
| `POST /internal/gateway/messages:stream` | `webadapter/adapter.py:45` | 触发 agentrun，间接调用 `recorder.start/finish/fail` |
| `GET /internal/conversations/{session_id}` | `conversation/http.py:30` | 返回体中 `usage` 字段即 `reader.session_summary` 的结果（camelCase） |

**内部方法调用方**：

| usage 方法 | 调用方 |
| --- | --- |
| `Recorder.start(run)` | `agentrun/service.py:96`（`Service.run`，运行被接受时） |
| `Recorder.finish(run, tokens)` | `agentrun/service.py:168`（`Service._run`，执行完成后） |
| `Recorder.fail(request_id)` | `agentrun/service.py:181,191`（取消 / 异常收尾） |
| `Reader.session_summary(user_id, session_id)` | `conversation/history.py:50`（`History.get`） |
| `Reader.status(user_id, request_id)` | **未发现调用方**（仅定义于 `reader.py:16`；`agentrun.Service.status` 与 `gateway.Service.status` 是内存态活跃任务查询，不经过 usage 仓储；Go 版 `Reader.Status` 亦未见 Python 侧接线，待确认是否遗留接口） |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

- **表结构**：`usage_runs`（`store.py:31-52`）—— `request_id TEXT PRIMARY KEY`（以运行 ID 为主键，天然防重）、`user_id` / `session_id` / `model_id` NOT NULL、`prompt_tokens` / `cached_prompt_tokens` / `cache_reported`（INTEGER NOT NULL DEFAULT 0，标记缓存用量是否被供应商上报）/ `usage_reported`（INTEGER NOT NULL DEFAULT 1，标记这次用量是供应商上报还是本地估算；旧库通过 `PRAGMA table_info` + `ALTER TABLE` 幂等补列）/ `completion_tokens` / `total_tokens`、`status` NOT NULL、`started_at`（DEFAULT CURRENT_TIMESTAMP）、`completed_at`。索引 `usage_runs_session_index`（`store.py:47-49`）覆盖 `(user_id, session_id, status)`，服务会话汇总查询。
- **状态机（隐式）**：`RUNNING = "running"` / `COMPLETED = "completed"` / `FAILED = "failed"`（`store.py:12-14`）。状态迁移靠 SQL 的 `WHERE status = 'running'` 约束：`finish` 只更新 running 行，`fail` 只把 running 行置为 failed（对已 completed 的行无效，幂等）。
- **防重复**：`start` 用 `INSERT ... ON CONFLICT(request_id) DO NOTHING` + `rowcount != 1` 判定重复并抛 `RunAlreadyExists`。
- **会话汇总口径**（`store.py:115-160`）：只聚合 `status='completed'` 的行；总量维度 `SUM(total_tokens)` / `SUM(completion_tokens)` 覆盖该会话全部已完成运行。缓存维度**只在「自己上报过缓存」的子集上聚合**（`cache_reported=1` 的行）：分子 `SUM(cached_prompt_tokens)`、分母 `SUM(prompt_tokens)` 同源，仅统计这些行，`cache_hit_rate = cached / cache_prompt`；若没有任何行上报过缓存，三个缓存字段为 `None`。**旧实现要求全部行都上报，导致一次本地估算就让整个会话的缓存统计清零**；现改为部分聚合 + 覆盖度字段（`total_runs` / `cache_reported_runs` / `estimated_runs`）表达可信范围，由前端标注「覆盖 M/K 次运行」「含 N 次本地估算」。
- **驼峰输出**：`Summary` 通过 `ConfigDict(alias_generator=to_camel, populate_by_name=True)`（`types.py:37`）序列化为 camelCase（如 `totalTokens`、`cacheHitRate`），与前端及 Go 版 JSON 对齐。

### ② 配置项

**无**。本模块不读取任何环境变量或配置文件；模块唯一输入是组合根传入的 `aiosqlite.Connection`。`main.py` 中的 `EDITH_E2B_TEMPLATE`（sandbox）、`EDITH_COS_*`（images）等环境变量与本模块无关。

### ③ 异常处理

| 异常 | 抛出位置 | 处理策略 |
| --- | --- | --- |
| `ValueError("usage database is required")` | `module.py:26`（`new_module`） | 组合根装配时参数错误；`main.py` 的 `open_database` 正常时不会触发 |
| `ValueError("requestID, userID, sessionID, and modelID are required")` | `store.py:53`（`start`） | 参数校验兜底；agentrun 在调用前已做 `_validate_request`（`agentrun/service.py:231`），正常流程不触发 |
| `RunAlreadyExists` | `store.py:63`（`start`） | **映射业务错误**：`agentrun/service.py:97-99` 捕获后返回 `Error("request_conflict", "agent run already exists")`，最终表现为 HTTP 冲突响应 |
| `RunNotFound` | `store.py:88`（`finish`）、`store.py:106`（`status`） | `finish` 抛出的 `RunNotFound` 在 `Service._run` 的 `except Exception` 分支（`agentrun/service.py:190-199`）中被兜底：调用 `fail` 并推送 `run.error` 事件（类型 `internal_error`）；`status` 的 `RunNotFound` 暂无调用方 |

无显式日志记录；错误信息通过 agentrun 的 `Error`/`APIError` 结构随事件流转。降级策略集中在 `session_summary`（缓存数据不全时返回基础汇总而非报错）。

### ④ 扩展点

- **新增用量维度**（如 reasoning tokens）：扩展 `usage_runs` 表结构（`create_schema` 加列，注意已有库的迁移）、`Tokens` dataclass、`Store.finish` 写入与 `Store.session_summary` 聚合、`Summary` 模型与序列化字段。
- **新增汇总口径**（按时间段、按模型、按天）：在 `Store` 增加新的聚合方法，`Reader` 暴露对应方法，由 conversation 或新模块调用；现有 `(user_id, session_id, status)` 索引可按需增加覆盖列或新索引。
- **换存储后端**：`Recorder` / `Reader` 只依赖 `Store` 的方法，可替换 `Store` 实现（如 PostgreSQL），或拆分出仓储接口；`new_module` 工厂是唯一装配点。
- **新增展示入口**：本模块无 HTTP 层，若需对外暴露用量明细接口，可在本模块新增 `http.py` 并在 `main.py` 注册路由，或在 conversation 等既有路由的返回体中扩展字段。
- **新增异常映射**：调用方（如 agentrun）捕获仓储层新异常并映射为对应 `Error` 类型即可，仓储层保持只抛领域异常。
