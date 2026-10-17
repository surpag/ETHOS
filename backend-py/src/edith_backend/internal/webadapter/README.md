# webadapter 模块：Web 渠道 HTTP/SSE 适配器

## 第一部分：模块名称与简介

**模块名**：`webadapter`（位于 `backend-py/src/edith_backend/internal/webadapter/`）

**一句话定位**：把 Web BFF 的 HTTP/SSE 协议适配为 Gateway 调用——只理解 HTTP、JSON 和 SSE，不关心 Agent 运行细节。

**对应 Go 版源码路径**：`EDITH/backend-v2/internal/webadapter/`（`adapter.go` + `stream.go` + `control.go` + `types.go`）。Python 版把 Go 版三个源文件合并进 `adapter.py`，`types.py` 对应 `types.go`。

**文件清单**：

| 文件名 | 一句话职责 |
|---|---|
| `adapter.py` | 适配器主体：定义 `Adapter` 类、三条 HTTP 路由、SSE 事件生成器与错误映射（对应 Go 版 `adapter.go` + `stream.go` + `control.go`） |
| `types.py` | 定义 Web BFF 发来的流式消息契约 `MessageRequest`（Pydantic v2 模型，camelCase 别名，对应 Go 版 `types.go`） |
| `__init__.py` | 模块入口，导出 `Adapter`、`MessageRequest` |

## 第二部分：架构设计

### ① 分层

本模块按职责可划分为四层，全部位于 `adapter.py` 与 `types.py` 两个文件中：

| 分层 | 载体 | 说明 |
|---|---|---|
| HTTP 边界层 | `Adapter` 类 + `APIRouter`（`adapter.py` `_build_router()`） | 声明三条路由，接收/返回 HTTP 请求与响应 |
| 协议转换层 | `event_generator` 闭包、`_gateway_error_to_http`（`adapter.py`） | 把 `agentstream.Event` 序列化为 SSE 帧；把 `agentrun.Error` 映射为 HTTP 错误 |
| 服务调用层 | `Adapter.gateway` 属性（`adapter.py` `__init__`） | 调用 `gateway.Service` 的 `run` / `status` / `cancel` |
| 类型定义层 | `types.py` `MessageRequest` | 请求体契约的声明与校验 |

模块本身不持有数据访问层：数据读写发生在下游 `agentrun`（会话、用量等）与 `database` 层，本模块只做协议适配。

### ② 设计模式

| 模式 | 代码位置 | 说明 |
|---|---|---|
| 适配器模式（Adapter） | `adapter.py` `Adapter` 类（模块名即体现） | 把 Web BFF 的 HTTP/SSE 协议适配为 Gateway 的中性调用，隔离协议与领域逻辑 |
| 依赖注入（构造器注入） | `adapter.py` `Adapter.__init__(self, gateway: GatewayService)` | Gateway 由组合根显式注入，`Adapter` 不自行创建依赖 |
| 工厂函数（装配侧） | 本模块未定义；组合根 `main.py` 使用 `new_gateway_service(...)` 创建 Gateway 后传入 `Adapter` | 体现"创建与使用分离"的显式装配 |
| 错误翻译/映射 | `adapter.py` `_gateway_error_to_http` | 把领域错误（`agentrun.Error.type`）翻译为 HTTP 状态码 + 统一错误响应，属于适配器模式的子职责 |
| 哨兵模式（None 哨兵） | `adapter.py` `event_generator` 中 `event is None: break` | `Stream.events` 队列中的 `None` 表示事件流结束（对应 Go 版 channel 关闭），消费方以此终止 SSE 流 |

未使用：仓储、策略、状态机（任务状态管理在下游 `agentrun` 模块内部，本模块只查询 `Status.request_id` / `Status.status` 两个字段）。

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
|---|---|---|
| `Adapter` | `adapter.py` | Web 渠道入口；`__init__(self, gateway: GatewayService) -> None`，注入 Gateway 并构建 `self.router` |
| `Adapter._build_router` | `adapter.py` | `_build_router(self) -> APIRouter`，声明三条路由并返回（在 `__init__` 中调用） |
| `Adapter.stream_message`（路由处理器） | `adapter.py` | `POST /internal/gateway/messages:stream`；把 `MessageRequest` 转换为 `gateway.IncomingMessage`（`channel=WEB_CHANNEL`）调用 `gateway.run`，成功则返回 SSE `StreamingResponse` |
| `Adapter.run_status`（路由处理器） | `adapter.py` | `GET /internal/gateway/runs/{request_id}`（查询参数 `userId`）；调用 `gateway.status`，返回 `{"requestId": ..., "status": ...}` |
| `Adapter.cancel_run`（路由处理器） | `adapter.py` | `POST /internal/gateway/runs/{request_id}/cancel`（查询参数 `userId`）；调用 `gateway.cancel`，无返回体 |
| `event_generator`（闭包） | `adapter.py` | 从 `stream.events`（`asyncio.Queue[Event \| None]`）消费事件；`None` 哨兵结束；每个事件序列化为 `data: {...}\n\n` 帧 |
| `_gateway_error_to_http`（模块级函数） | `adapter.py` | `(error: Error) -> EDITHError`；按 `error.type` 映射 HTTP 状态码并构造统一错误（对应 Go 版 `writeGatewayError`，见 `control.go`） |
| `MessageRequest`（Pydantic 模型） | `types.py` | 请求体契约：`request_id` / `user_id` / `session_id` / `message` / `image_ids` / `upload_paths` / `model_id` / `reasoning_option_id`；`alias_generator=to_camel`、`populate_by_name=True`、`extra="forbid"` |

## 第三部分：与其他模块的联系

### ① 依赖关系

| 依赖 | 用途 |
|---|---|
| `..gateway.service`（`Service as GatewayService`） | 构造器注入的下游服务，提供 `run` / `status` / `cancel` 三个方法 |
| `..gateway.types`（`WEB_CHANNEL`、`IncomingMessage`） | 渠道常量（`"web"`）与渠道消息载体，构造 `gateway.run` 的入参 |
| `..agentrun.types`（`Error`） | 下游返回的错误类型，作为 `_gateway_error_to_http` 的输入 |
| `..agentstream.types`（`Event`） | 渠道中性的流式事件模型，SSE 序列化对象 |
| `..http_utils`（`EDITHError`） | 业务错误异常，抛出后由全局异常处理器转为 `{"type","message"}` JSON 响应 |
| 外部 SDK：`fastapi`（`APIRouter`、`StreamingResponse`）、`pydantic`（`BaseModel`、`ConfigDict`、`Field`、`alias_generators.to_camel`） | HTTP 路由框架与请求体校验/序列化 |

装配关系（`main.py` 组合根）：`gateway = new_gateway_service(users.bindings, agent_runs)` → `web = Adapter(gateway)` → `app.include_router(web.router)`（`main.py` 第 99~100、112 行）。

### ② 数据流向

一次典型"Web 流式消息"业务流转：

```
Web BFF（Clerk 注入 userId）
   │  POST /internal/gateway/messages:stream  (JSON, camelCase)
   ▼
adapter.py stream_message
   │  解析 MessageRequest → 构造 gateway.IncomingMessage(channel="web", ...)
   ▼
gateway.Service.run()
   │  web 为可信渠道 → 不做 bindings 身份转换，直接以 external_user_id 为 user_id
   │  （gateway/service.py _is_trusted_channel：web、cron 均可信）
   ▼
agentrun.Service.run() → 返回 Stream(asyncio.Queue[Event | None])
   │
   ▼
adapter.py event_generator
   │  消费队列：Event.model_dump_json(exclude_none=True, by_alias=True) → camelCase
   ▼
SSE 响应  data: {"type":"delta","sessionId":...,"requestId":...,"delta":"..."}\n\n
   │  直到收到 None 哨兵 → 关闭事件流
   ▼
Web BFF / 前端（按 camelCase 字段读取事件）
```

状态查询 / 取消的数据流向：`GET|POST /internal/gateway/runs/{request_id}?userId=...` → `gateway.status / cancel` → `agentrun.status / cancel` → 成功返回 `{"requestId","status"}`（查询）或空响应（取消）；失败则抛 `EDITHError`。

### ③ 接口调用

**本模块暴露的 HTTP 路由**（注册于 `Adapter.router`，由 `main.py` `app.include_router(web.router)` 挂载）：

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/internal/gateway/messages:stream` | 流式消息：请求体为 `MessageRequest`，响应为 SSE 事件流 |
| `GET` | `/internal/gateway/runs/{request_id}` | 查询活跃任务状态，查询参数 `userId` |
| `POST` | `/internal/gateway/runs/{request_id}/cancel` | 取消活跃任务，查询参数 `userId` |

**内部方法**：

- `Adapter.__init__(gateway)` —— 被 `main.py` 组合根调用（装配）。
- `Adapter.gateway.run / status / cancel` —— 本模块调用 `gateway.Service` 的同名方法；`gateway` 又被 `cronadapter`（`main.py` 中 `CronAdapter(gateway)`）等其他渠道适配器共用，即 Gateway 是所有 Agent 渠道的唯一入口。

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **协议适配**：模块只关心 HTTP/JSON/SSE 与 Gateway 中性调用的互相转换，不感知 Agent 内部逻辑。
2. **可信渠道直通**：`WEB_CHANNEL = "web"` 属于可信渠道（`gateway/service.py` `_is_trusted_channel`），Gateway 不做 bindings 身份转换，直接用 `external_user_id` 作为 EDITH `user_id`；`userId` 必须由 Web BFF 从 Clerk 注入（`types.py` 文档注释），浏览器不能自行提供。
3. **SSE 序列化契约**：`Event.model_dump_json(exclude_none=True, by_alias=True)` 输出 camelCase（`assistantId` / `blockId` / `toolCallId` / `sessionUsage` 等），与 Go 版 `agentstream.Event` 的 json tag 保持一致，前端按 camelCase 读取；`exclude_none=True` 省略空字段（对齐 Go 的 `omitempty`）。
4. **SSE 响应头**：`media_type="text/event-stream"`，并设置 `Cache-Control: no-cache, no-transform` 与 `X-Accel-Buffering: no`（防止 Nginx 缓冲导致流不实时），与 Go 版 `stream.go` 一致。
5. **事件流终止**：`Stream.events` 队列的 `None` 哨兵表示事件流结束（对应 Go 版 channel 关闭），消费到即 `break` 关闭 SSE 流。
6. **请求体严格校验**：`MessageRequest` 配置 `extra="forbid"`，未知字段会触发 `RequestValidationError`，由全局处理器统一返回 `400 invalid_request`（对齐 Go 版 `ReadJSON` 的 `DisallowUnknownFields` 行为）。
7. **与 Go 版的一个行为差异**：取消路由 `cancel_run` 处理器返回 `None`（FastAPI 默认 200 空响应），而 Go 版 `control.go` 显式返回 `204 No Content`——前端若依赖 204 需注意（见 ③ 异常处理）。

### ② 配置项

**无**。`adapter.py` / `types.py` 未读取任何环境变量或配置；组合根 `main.py` 读取的 `EDITH_E2B_TEMPLATE`（sandbox）、`EDITH_COS_*`（images）等属于其他模块，与本模块无关。

### ③ 异常处理

| 异常来源 | 处理策略 | 代码位置 |
|---|---|---|
| `gateway.run / status / cancel` 返回的 `agentrun.Error`（非异常，走返回值） | `_gateway_error_to_http` 按 `error.type` 映射状态码后 `raise EDITHError`：`invalid_request`→400；`session_busy` / `request_conflict`→409；`identity_not_bound`→403；`not_found`→404；其余（含 `internal_error`）默认 500 | `adapter.py` `_gateway_error_to_http` |
| `EDITHError` | 由 `http_utils.register_error_handler`（`main.py` 注册）统一转为 `{"type","message"}` JSON 响应 | `http_utils.py` |
| `RequestValidationError`（请求体校验失败，含未知字段） | 统一转为 `400 {"type":"invalid_request","message":"invalid request"}` | `http_utils.py` |

无降级逻辑。SSE 客户端断开后的事件写失败在 Python 版未做显式处理（Go 版 `stream.go` 用 `clientConnected` 标志停止继续写流；Python 版依赖 `StreamingResponse` 的异步迭代在客户端断开时自行终止，行为差异未在代码中显式管理，待确认是否需要）。

### ④ 扩展点

| 扩展需求 | 做法 | 涉及文件 |
|---|---|---|
| 新增 HTTP 路由（如 Web 端新操作） | 在 `Adapter._build_router()` 内追加 `@router` 装饰的路由处理函数 | `adapter.py` |
| 新增 / 调整请求字段 | 修改 `MessageRequest` 字段（注意 `extra="forbid"` 会拒绝未声明的字段，需同步更新） | `types.py` |
| 新增 SSE 事件字段 | 在 `agentstream.types.Event` 扩展字段，`by_alias=True` 自动输出 camelCase，前端无需改协议层 | `agentstream/types.py` |
| 调整错误→HTTP 状态码映射 | 修改 `_gateway_error_to_http` 的映射分支 | `adapter.py` |
| 新增渠道（Telegram / WhatsApp 等） | 参考 `cronadapter` 模式：新建渠道适配器，注入同一个 `gateway.Service`，在 `main.py` 组合根装配并 `include_router` | 新模块 + `main.py` |
| 新增 provider / 工具 / 存储 | 不属于本模块职责，在 `models` / `tools` / `agentrun` / `database` 层扩展 | 其他模块 |

---

**摘要**：本 README 覆盖模块定位、四层架构（HTTP 边界 / 协议转换 / 服务调用 / 类型定义）、6 种设计模式与 3 条路由的完整清单，梳理了与 gateway/agentrun/agentstream/http_utils 的依赖与数据流。值得注意：① 取消路由 Python 返回 200 空响应而 Go 返回 204；② `extra="forbid"` + 全局校验处理器复刻了 Go 的 DisallowUnknownFields 语义；③ `userId` 查询参数保持 camelCase（与 Go 一致，未转 snake_case）；④ SSE 客户端断开处理 Python 版未显式管理，与 Go 版 `clientConnected` 逻辑存在差异，已标注"待确认"。
