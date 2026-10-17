# conversation 模块（会话历史与 Timeline 投影）

> 模块名：`edith_backend.internal.conversation`（包路径 `src/edith_backend/internal/conversation/`）
>
> 一句话定位：读取 SQLite 中持久化的会话消息记录，将其投影为浏览器可渲染的 Timeline 结构并附带会话用量汇总，通过两个内部 HTTP 路由供 web-v1 BFF 代理调用（**只读**，本模块不写任何数据）。
>
> 对应 Go 版源码路径：`backend-v2/internal/conversation/`（`http.go`、`routes.go`、`types.go`、`history.go`、`timeline_projection.go`、`module.go`），模块一一对应。

## 一、模块名称与简介

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块公共出口：导出 `HTTP`、`Module`、`new_module` 及全部 Timeline 类型（`__all__`） |
| `http.py` | HTTP 边界：定义 `GET /internal/conversations` 与 `GET /internal/conversations/{session_id}` 两个路由（对应 Go `http.go` + `routes.go`） |
| `types.py` | Pydantic v2 类型定义：会话摘要、Timeline 各类块与 HTTP 响应模型（对应 Go `types.go`） |
| `history.py` | 服务层与投影：`History.list/get` 业务编排、标题截取 `_title`、DB 记录 → Timeline 投影 `_timeline`（对应 Go `history.go` + `timeline_projection.go`） |
| `module.py` | 模块组装入口：`Module` 类与 `new_module` 工厂函数（对应 Go `module.go`） |

## 二、架构设计

### 2.1 分层

按代码实际分层，本模块是"薄读取"模块，数据访问层完全复用 session / usage 两个模块：

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 组合根（装配） | `main.py` 的 `lifespan` | 创建 `SessionStore`、`usage.reader`，调用 `new_conversation_module(...)` 组装，并 `app.include_router(conversations.http.router)` 注册路由 |
| 模块组装 | `module.py` | `new_module(store, usage)` 校验依赖非空，组装 `History` → `HTTP` |
| HTTP 边界 | `http.py` 的 `HTTP` 类 | FastAPI `APIRouter`；参数校验、业务异常 → `EDITHError` 映射 |
| 服务层 | `history.py` 的 `History` 类 | `list`/`get` 编排：调 store 取记录、调 usage 取用量、做标题截取与 Timeline 投影 |
| 数据访问层（复用外部模块） | `session.store.SessionStore`、`usage.reader.Reader` | 本模块**不直接执行 SQL**，全部经构造注入的访问器读取 |
| 类型定义 | `types.py` | Pydantic 模型；Timeline 块多态用 Union 联合类型表达 |

### 2.2 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入 | `history.py` `History.__init__(store, usage)`、`http.py` `HTTP.__init__(history)` | 依赖在构造函数注入，由组合根 `main.py` 显式装配（"依赖由调用方持有"，对应 Go 版 `Dependencies` 结构体） |
| 工厂方法 | `module.py` `new_module(store, usage)` | 对应 Go 版 `New(deps)`：集中创建 `History`/`HTTP` 并校验依赖（`store`/`usage` 为 `None` 时抛 `ValueError`） |
| 适配器 | `http.py` `HTTP._build_router()` | 把 `History` 服务适配为 FastAPI 路由（对应 Go 版 `HTTP` 结构体 + `Register(mux)`） |
| 多态（Union 判别） | `types.py` `Timeline.blocks: list[UserBlock \| AssistantBlock \| ErrorBlock]` | 用 Pydantic 联合类型表达 Timeline 块多态，对应 Go 版 `TimelineBlock` interface |
| 命名别名适配 | `types.py` 各模型 `ConfigDict(alias_generator=to_camel, populate_by_name=True)` | 字段序列化为 camelCase（`updatedAt`/`createdAt`/`imageIds` 等），对齐 Go 版 JSON tag 与前端契约 |

### 2.3 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `HTTP` | `http.py` | HTTP 边界。`__init__(history: History)`；`_build_router() -> APIRouter`；`list_conversations(userId: str = "") -> ConversationListResponse`；`get_conversation(session_id: str, userId: str = "") -> ConversationResponse` |
| `History` | `history.py` | 服务层。`__init__(store: SessionStore, usage: Reader)`；`async list(user_id: str) -> list[Conversation]`；`async get(user_id: str, session_id: str) -> ConversationResponse` |
| `ConversationNotFound` | `history.py` | 会话不存在的业务异常（继承 `Exception`），HTTP 层捕获后映射为 404 |
| `_title` | `history.py` | `_title(first_message: str) -> str`：会话标题截取，上限 `_TITLE_LIMIT = 18`，空白内容返回 `"新对话"` |
| `_timeline` | `history.py` | `_timeline(records: list[MessageRecord]) -> Timeline`：DB 消息记录 → Timeline 投影 |
| `Module` | `module.py` | 模块对外能力集合。`__init__(http: HTTP)` |
| `new_module` | `module.py` | `new_module(store: SessionStore, usage: Reader) -> Module`：组装工厂，依赖为空抛 `ValueError` |
| `Conversation` / `ConversationListResponse` | `types.py` | 侧边栏会话摘要（`id/title/updatedAt`）/ 会话列表 HTTP 响应 |
| `ConversationResponse` | `types.py` | 单会话响应：`timeline: Timeline` + `usage: Summary`（复用 `usage.types.Summary`） |
| `Timeline` / `UserBlock` / `UserImage` | `types.py` | Timeline 结构；用户消息卡片；消息内引用的 EDITH 图片 |
| `AssistantBlock` / `AssistantContentBlock` | `types.py` | AI 回复卡片；`reasoning`/`text`/`tool` 展示子块 |
| `ErrorBlock` | `types.py` | 历史运行错误卡片（已定义，见 4.4：当前投影未使用） |

## 三、与其他模块的联系

### 3.1 依赖关系

**内部模块（import 来源）**：

| 依赖 | 用途 |
| --- | --- |
| `..session.store.SessionStore` | 读会话消息：`list_sessions(user_id)` 返回 `[(session_id, first_message, updated_at)]`；`get_records(user_id, session_id)` 返回 `list[MessageRecord]`（数据源为 `chat_messages` 表） |
| `..session.types.MessageRecord` | 消息记录 dataclass（`id/role/content/reasoning/image_ids/created_at`），投影输入 |
| `..usage.reader.Reader` | 读用量：`session_summary(user_id, session_id) -> Summary`（聚合 `usage_runs` 表） |
| `..usage.types.Summary` | 用量汇总模型（`totalTokens`、`completionTokens`、`cacheHitRate` 等），随 `ConversationResponse` 返回 |
| `..http_utils.EDITHError` | 统一业务错误三元组（`status_code` + `error_type` + `message`），HTTP 层抛错用 |

**外部 SDK**：`fastapi`（`APIRouter` 定义路由）；`pydantic` v2（`BaseModel`、`ConfigDict`、`Field`、`alias_generators.to_camel` 做类型定义与 camelCase 序列化）。除此之外无其他外部依赖。

### 3.2 数据流向

一次典型业务流转（web-v1 前端查看会话列表 / 会话详情）：

```text
web-v1 前端
   │  GET /api/conversations 或 /api/conversations/{sessionId}
   ▼
web-v1 BFF（Clerk 认证取 userId，转发到 EDITH_RUNTIME_URL，默认 http://127.0.0.1:8080）
   │  GET /internal/conversations?userId=...
   │  GET /internal/conversations/{session_id}?userId=...
   ▼
conversation/http.py（校验 userId（、sessionId）非空，缺失 → EDITHError 400）
   ▼
conversation/history.py
   ├─ list: SessionStore.list_sessions ──► SQL 读 chat_messages（子查询取首条 user 消息作标题候选，
   │         MAX(created_at) 排序）──► _title 截断 ──► list[Conversation]
   └─ get : SessionStore.get_records ──► 记录为空 → ConversationNotFound(→404)
            + usage.Reader.session_summary ──► usage store 聚合 usage_runs（仅 status='completed' 的行）──► Summary
            + _timeline(records) ──► Timeline
   ▼
返回 ConversationListResponse{conversations} / ConversationResponse{timeline, usage}
（FastAPI response_model 序列化，字段 camelCase）
```

- **入**：HTTP 请求（query 参数 `userId`、路径参数 `session_id`）。
- **出（内部）**：经注入的 `SessionStore`/`Reader` 读 SQLite 表 `chat_messages`（session 模块建表）与 `usage_runs`（usage 模块建表）。
- **出（对外）**：两个内部路由的 JSON 响应，仅被 web-v1 BFF 消费。

### 3.3 接口调用

**本模块暴露的 HTTP 路由**：

| Method | Path | 参数 | 响应模型 |
| --- | --- | --- | --- |
| `GET` | `/internal/conversations` | query：`userId`（必填，空白 → 400） | `ConversationListResponse` |
| `GET` | `/internal/conversations/{session_id}` | path：`session_id`；query：`userId`（任一空白 → 400） | `ConversationResponse` |

**被谁调用**：

- web-v1 BFF 代理：`web-v1/app/api/conversations/route.ts`（会话列表）与 `web-v1/app/api/conversations/[sessionId]/route.ts`（会话详情）——Clerk 认证后把 `userId` 拼入 query 转发到 runtime。
- 组合根：`main.py` 中 `conversations = new_conversation_module(session_store, usage.reader)` 后 `app.include_router(conversations.http.router)` 注册到 FastAPI 应用。

**内部方法调用链**：`main.py` → `new_module` → `History(store, usage)`、`HTTP(history)` → HTTP handler 调 `history.list` / `history.get` → `store.list_sessions` / `store.get_records` + `usage.session_summary`。

## 四、模块内容扩展说明

### 4.1 核心业务逻辑要点

- **标题生成**：取会话第一条非空 `user` 消息（`SessionStore.list_sessions` 的 SQL 子查询保证），`strip` 后按码位截断到 18 字符（`_TITLE_LIMIT`），空则返回 `"新对话"`。
- **Timeline 投影规则**（`_timeline`）：
  - `role == "user"` → `UserBlock`，`id` 形如 `user_{record.id}`；`image_ids` 逐个转 `UserImage(id=...)`。
  - 其余（assistant）→ `AssistantBlock`，`id` 形如 `assistant_{record.id}`；`reasoning` 非空追加 `type="reasoning"` 子块（`assistant_{id}_reasoning`），`content` 非空追加 `type="text"` 子块（`assistant_{id}_text`）。
- **稳定 id**：所有块 id 由 DB 自增主键 `record.id` 拼接派生，刷新/重复请求保持稳定。
- **会话不存在判定**：`get_records` 返回空列表即视为会话不存在（抛 `ConversationNotFound`），而非存储层报错。
- **只读模块**：本模块不新增/修改任何消息或用量记录，写路径在 session（`SessionStore.add_user_message`/`add_assistant_message`）与 usage（`recorder`）模块。

### 4.2 配置项

**无**。本模块代码未读取任何环境变量或配置（`EDITH_E2B_TEMPLATE`、`EDITH_COS_*` 等是组合根 `main.py` 中 sandbox/images 等其他模块的配置；运行监听地址由组合根 `config` 模块管理）。前端 BFF 侧另有 `EDITH_RUNTIME_URL`（web-v1 配置，不属于本模块）。

### 4.3 异常处理

| 异常 | 触发条件 | 处理策略 |
| --- | --- | --- |
| `EDITHError(400, "invalid_request", ...)` | `userId`（及 `session_id`）缺失或空白 | 直接抛出，由全局统一错误处理器返回 `400 {"type":"invalid_request","message":...}` |
| `ConversationNotFound` | `get_records` 无记录 | HTTP 层捕获后转 `EDITHError(404, "conversation_not_found", ...)` |
| 未知异常（如 SQL/存储错误） | store/usage 读取失败 | **未发现显式捕获**：会冒泡，由 FastAPI 默认处理（500 空响应）。注意 Go 版对这类错误映射为 `500 conversation_list_failed` / `conversation_read_failed`，Python 版未实现该映射（待确认是否属有意简化） |
| `RequestValidationError` | FastAPI 请求体验证失败 | 由 `http_utils.register_error_handler`（`main.py` 全局注册）统一转 `400 invalid_request`，模块复用 |

所有错误响应统一为 `{"type": ..., "message": ...}` JSON（`http_utils.py` 注册的全局处理器，与 Go 版 `httpx` 契约一致）。

### 4.4 扩展点

- **新增 Timeline 块类型**：在 `types.py` 定义新 Pydantic 模型（带 `type` 判别字段），加入 `Timeline.blocks` 的 Union；再到 `history.py._timeline` 中按记录生成对应块。
- **启用 tool / error 展示**：`AssistantContentBlock` 已预留 `tool_name/arguments/status/result` 字段、`ErrorBlock` 已定义但当前投影均未使用——需要扩展 `_timeline` 的生成逻辑；由于当前 `chat_messages` 未持久化工具调用/运行错误，还需先在 session 写入侧（或新增存储字段）落数据。
- **新增路由**：在 `http.py` 的 `_build_router()` 内追加 `@router.xxx` 处理器即可，无需改其他层。
- **替换数据源/存储**：`History` 构造函数面向 `SessionStore`/`Reader` 鸭子类型注入，可整体替换为其他实现；或扩展 `session.store` 提供更多查询方法。
- **变更响应契约**：只改 `types.py` 模型字段与 alias 配置即可，HTTP 层与投影逻辑通常无需变动。
- **新增内部能力**：在 `module.py` 的 `Module` 上增加字段并在 `new_module` 中装配（沿用工厂模式）。
