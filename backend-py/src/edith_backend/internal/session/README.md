# session 模块（会话消息持久化）

## 第一部分：模块名称与简介

### 模块名称

`edith_backend.internal.session`

### 一句话定位

会话消息持久化模块：用 SQLite 表 `chat_messages` 持久化每个用户、每个会话的对话消息，供 AgentRun 拼装模型上下文、conversation 投影 Timeline 与侧边栏会话列表。

### 对应 Go 版源码路径

Go 版 backend-v2 **不存在** `internal/session` 目录（经核对 `D:/agent/EDITH/EDITH/backend-v2/internal/` 下无同名模块）。按 `__init__.py` 注释与 Go 版 `cmd/server/main.go` 的对照，该职责在 Go 版由 **trpc-agent-go 框架的 `session/sqlite` service** 承担：

- Go 版 `cmd/server/main.go` 通过 `sessionsqlite.NewService(sessionDB, sessionsqlite.WithSummarizer(...))` 创建框架会话服务，注入 conversation（`conversation.New(conversation.Dependencies{... Sessions: rawSessions ...})`）与 agentrun（`runner.WithSessionService(imageSessions)`）。
- Python 版采用「方案 B」：不依赖 trpc-agent-go，自行用 SQLite 表持久化对话消息，由本模块的 `SessionStore` 直接读写。

> 说明：Go 版会话服务由框架事件流（`session.Event`）驱动，Python 版为轻量消息表方案，两者职责对齐但存储模型不同，README 以 Python 实现为准。

### 文件清单

| 文件 | 一句话职责 |
| --- | --- |
| `schema.py` | 定义 `chat_messages` 表结构与索引，提供 `create_schema(db)` 建表函数 |
| `store.py` | 定义 `SessionStore`，封装消息的插入与各类查询（SQLite 访问器） |
| `types.py` | 定义 `MessageRecord` 数据类（一条持久化消息的记录类型） |
| `__init__.py` | 模块导出与定位说明（导出 `MessageRecord`、`SessionStore`、`create_schema`） |

## 第二部分：架构设计

### ① 分层

本模块是纯内部数据访问模块，**不包含 HTTP 边界**（无 FastAPI 路由），整体为两层：

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 数据访问层（服务层） | `store.py` 的 `SessionStore` | 对 SQLite 连接的读写封装：插入消息、读取历史、列出会话摘要 |
| 类型定义层 | `types.py` 的 `MessageRecord` | 持久化消息的记录类型（含转 OpenAI 消息格式的方法） |
| 表结构定义 | `schema.py` | 建表 SQL 语句集合与 `create_schema(db)` 执行入口 |

依赖方向：`store.py` → `types.py`（`from .types import MessageRecord`）；`schema.py` 独立无内部依赖。

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `store.py` `SessionStore.__init__(self, db: aiosqlite.Connection)` | 数据库连接由组合根 `main.py` 打开后注入，store 不负责连接生命周期 |
| 仓储模式（Repository） | `store.py` 整个 `SessionStore` 类 | 将表级 SQL 封装为领域方法（`add_user_message` / `get_messages` 等），调用方不接触 SQL |
| 工厂函数 | `schema.py` `create_schema(db)` | 组装并执行建表语句集合，供启动期统一建表 |
| 模块门面导出 | `__init__.py` | 统一导出公共符号（`MessageRecord`、`SessionStore`、`create_schema`），隐藏内部实现 |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `create_schema(db)` | `schema.py` | 执行建表与建索引语句并 commit：`async def create_schema(db: aiosqlite.Connection) -> None` |
| `SessionStore` | `store.py` | 会话消息 SQLite 访问器，构造器接收 `aiosqlite.Connection` |
| `SessionStore.add_user_message(user_id, session_id, content, image_ids=None)` | `store.py` | 插入一条 `role='user'` 消息（image_ids 为空时存 NULL） |
| `SessionStore.add_assistant_message(user_id, session_id, content, reasoning='')` | `store.py` | 插入一条 `role='assistant'` 消息（带可选推理内容） |
| `SessionStore._insert(user_id, session_id, role, content, reasoning, image_ids)` | `store.py` | 私有统一插入实现，`image_ids` 经 `json.dumps` 序列化后存储 |
| `SessionStore.get_messages(user_id, session_id)` | `store.py` | 返回 OpenAI 消息格式列表 `[{"role", "content"}]`（按 id 升序，供拼装模型上下文） |
| `SessionStore.get_records(user_id, session_id)` | `store.py` | 返回完整 `MessageRecord` 列表（含 reasoning、image_ids、created_at，供 Timeline 投影） |
| `SessionStore.list_sessions(user_id)` | `store.py` | 返回会话摘要 `[(session_id, first_message, updated_at)]`，按 `updated_at` 倒序 |
| `MessageRecord` | `types.py` | `@dataclass`：`id:int, role:str, content:str, reasoning:str='', image_ids:list[str]=field(default_factory=list), created_at:str=''` |
| `MessageRecord.to_openai_message()` | `types.py` | 转为 OpenAI 消息格式：`{"role": ..., "content": ...}` |

## 第三部分：与其他模块的联系

### ① 依赖关系

**本模块依赖（import 了哪些）：**

| 依赖 | 用途 |
| --- | --- |
| `aiosqlite`（外部 SDK） | 异步 SQLite 驱动，`store.py` / `schema.py` 中的连接类型与执行入口 |
| `.types`（内部，`store.py` 引入 `MessageRecord`） | 查询结果的结构化类型 |

**依赖本模块的模块（被谁 import）：**

| 模块 | import 内容 | 用途 |
| --- | --- | --- |
| `main.py`（组合根） | `SessionStore`、`create_schema` | 打开数据库后建表、实例化 `SessionStore(db)` 并注入 conversation / agentrun |
| `internal/agentrun/`（`service.py`、`execution.py`） | `..session.store.SessionStore` | 写用户消息、写 assistant 消息、读历史拼装上下文 |
| `internal/conversation/`（`module.py`、`history.py`） | `..session.store.SessionStore`、`..session.types.MessageRecord` | 读会话记录与摘要，投影 Timeline |

### ② 数据流向

一次典型业务流转（用户发起一次 AgentRun）中数据进出本模块的路径：

```
用户发起 AgentRun
  │  gateway → agentrun.Service.Start
  ▼
① 写用户消息：agentrun/service.py 调用 store.add_user_message(user_id, session_id, message, image_ids)
  │
  ▼
② 读历史拼上下文：agentrun/execution.py 调用 store.get_messages(user_id, session_id)，
   build_messages(system_instruction, history, user_message) 拼出 OpenAI 消息列表
  │
  ▼
③ 流式回复完成：execution.py 调用 store.add_assistant_message(user_id, session_id, content, reasoning)
   （工具调用循环中，若达到 MAX_TOOL_ROUNDS=8 仍持续调用工具，也按结束写入一次 assistant 消息）
  │
  ▼
前端查看会话
  ├─ 会话列表：conversation/history.py 调用 store.list_sessions(user_id) → 侧边栏摘要（首条 user 消息作标题候选）
  └─ 会话详情：conversation/history.py 调用 store.get_records(user_id, session_id) → Timeline 投影（含 reasoning / 图片 id）
```

写入方向：`agentrun` 负责写（用户消息 + assistant 消息）；读取方向：`agentrun` 读历史（OpenAI 格式）、`conversation` 读记录/摘要（Timeline 格式）。本模块自身不发起任何业务流转，是纯被动的存储访问器。

### ③ 接口调用

**本模块暴露的 HTTP 路由：无。**

session 是纯内部模块，`main.py` 中 `app.include_router(...)` 列表里没有任何 session 路由。

**本模块暴露的内部方法及调用方：**

| 方法 | 调用方（文件:行） | 场景 |
| --- | --- | --- |
| `add_user_message` | `agentrun/service.py:102` | AgentRun 启动时持久化用户消息 |
| `add_assistant_message` | `agentrun/execution.py:155`、`execution.py:186` | 流式回复完成 / 达到最大工具轮数时持久化 assistant 消息 |
| `get_messages` | `agentrun/execution.py:90` | 拼装模型上下文前读取会话历史 |
| `get_records` | `conversation/history.py:47` | 会话详情 Timeline 投影 |
| `list_sessions` | `conversation/history.py:35` | 会话列表侧边栏摘要 |
| `create_schema` | `main.py:54`（lifespan 内） | 启动建表 |

**session 数据最终暴露给用户的 HTTP 入口（由 conversation 承担）：**

- `GET /internal/conversations`（`conversation/http.py`）→ 经 `list_sessions` 输出会话列表
- `GET /internal/conversations/{session_id}`（`conversation/http.py`）→ 经 `get_records` 输出 Timeline（无记录时返回 404 `conversation_not_found`）

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **存储模型**：单表 `chat_messages`，列为 `id`（自增主键）、`user_id`、`session_id`、`role`、`content`、`reasoning`、`image_ids`、`created_at`；`user_id`/`session_id`/`id` 上有联合索引 `chat_messages_session_index`，支撑按用户+会话的时序读取。`role` 实际只写入 `"user"` / `"assistant"` 两种值（`MessageRecord.role` 注释亦如此）。
2. **空值约定**：`reasoning` 为空串时存 NULL（`reasoning or None`）；`image_ids` 为空列表时存 NULL，非空时经 `json.dumps` 存 JSON 文本；读取时 `json.loads` 还原，缺省为空列表。
3. **两条读取视图**：`get_messages` 只取 `role`/`content` 两列，输出 OpenAI 消息格式，直接喂给模型上下文；`get_records` 取全字段并还原为 `MessageRecord`，供 conversation 的 Timeline 投影（`_timeline` 把 user 消息映射为 `UserBlock`+图片，assistant 消息拆为 reasoning/text 两个 `AssistantContentBlock`）。
4. **会话列表语义**：`list_sessions` 按 `session_id` 分组，`first_message` 取每个会话第一条非空 user 消息（conversation 侧截断前 18 字作标题，超长取前 18 字符），`updated_at` 取 `MAX(created_at)`，按此倒序排列。
5. **写入时机**：用户消息在 AgentRun 启动阶段（usage 注册后）即落库；assistant 消息在流式结束或工具循环封顶（`MAX_TOOL_ROUNDS = 8`，见 `execution.py`）后落库，中间的工具调用过程不写消息。

### ② 配置项

**无。**

本模块不读取任何环境变量/配置（`EDITH_E2B_TEMPLATE` 等由 sandbox、images 等其他模块读取；数据库连接由 `main.py` 经 `config.database_path()` 打开后注入，属 database/config 模块职责，session 只接收连接对象）。

### ③ 异常处理

**模块内未发现显式异常处理逻辑**（无 try/except、无自定义异常、无日志记录），SQLite 错误（如连接关闭、约束冲突）会沿调用栈向上抛给调用方处理。相关降级/映射发生在**调用方**：

- `conversation/history.py` 定义 `ConversationNotFound`：`get_records` 返回空记录时抛出，由 `conversation/http.py` 映射为 HTTP 404 `conversation_not_found`。
- `conversation/module.py` 对缺失依赖（`store`/`usage` 为 None）抛出 `ValueError`，属组合根装配期校验，与本模块数据访问无关。

### ④ 扩展点

- **新增消息类型/字段**：在 `schema.py` 的 `_STATEMENTS` 中调整建表语句（注意 `CREATE TABLE IF NOT EXISTS` 不会迁移已有表，字段变更需另行处理，代码中未发现迁移机制）；同步更新 `store.py` 的 `_insert` 与 `get_records` 的 SELECT 列，以及 `types.py` 的 `MessageRecord`。
- **新增查询方法**：在 `SessionStore` 类内按现有模式新增 async 方法（`self._db.execute` + `fetchall` + `close`），如按会话分页、按角色过滤等。
- **替换存储后端**：保持 `SessionStore` 公开方法签名（`add_user_message` / `add_assistant_message` / `get_messages` / `get_records` / `list_sessions`），替换类实现即可，调用方（agentrun、conversation）无需改动。
- **新增路由**：session 模块本身不暴露 HTTP 路由；如需对外暴露消息查询接口，应在消费方（如 conversation）新增路由并复用 `SessionStore` 方法，或另建对外模块——当前代码未见此类接口。
- **新存储（如消息摘要/归档）**：可参考 `__init__.py` 注释中 Go 版 `WithSummarizer` 的角色，但 Python 版当前**未实现**会话摘要（`list_sessions` 仅取首条 user 消息作标题候选，无模型摘要），如需引入可作为独立能力在 session 或 agentrun 侧扩展（待确认是否有后续规划）。

---

## 摘要

- **写入内容**：为 `backend-py` 的 session 模块撰写了四大部分 README：模块简介与文件清单（4 个文件）、架构设计（两层结构 + 4 种模式 + 11 项关键类/函数表）、模块联系（依赖/数据流向/接口调用三小节）、扩展说明（业务要点/配置/异常/扩展点）。
- **值得注意**：Go 版 backend-v2 不存在 `internal/session` 目录，对应职责由 trpc-agent-go 框架 `session/sqlite` service 承担（`cmd/server/main.go`），Python 版为「方案 B」自有 SQLite 表方案。
- **值得注意**：session 模块不暴露任何 HTTP 路由，是纯内部仓储层；对外接口由 conversation（`GET /internal/conversations`、`GET /internal/conversations/{session_id}`）和 agentrun 承担。
- **值得注意**：本模块无配置项、无显式异常处理，唯一的状态映射（空记录 → 404）发生在调用方 conversation 模块；`get_messages` 与 `get_records` 是两种不同粒度的读取视图，分别服务模型上下文与 Timeline 投影。
