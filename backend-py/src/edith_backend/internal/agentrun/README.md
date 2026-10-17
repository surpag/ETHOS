# agentrun（Agent 执行入口与生命周期）

## 第一部分：模块名称与简介

**模块名**：`agentrun`

**一句话定位**：EDITH 唯一的 Agent 执行入口——聚合一次运行所需的全部配置（模型、供应商密钥、人格、Skill 摘要、图片、上传文件、MCP 工具），按「会话级串行」准入后启动流式对话与工具调用循环，并以渠道中性的 `Event` 事件流输出给调用方。

**对应 Go 版源码路径**：`backend-v2/internal/agentrun/`（`types.go`、`configuration.go`、`execution.go`、`stream.go`、`service.go`、`session_lanes.go`、`user_stops.go`。注意：Go 版的 `run_options.go`、`session_summary.go` 在 Python 版没有同名文件——前者被合并进 `ConfiguredRun`/`execution.py`，后者归入 `usage` 模块）。

**文件清单**（`src/edith_backend/internal/agentrun/`）：

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块出口：导出 `Dependencies`、`Service`、`Error`、`Request`、`Status`、`Stream` |
| `types.py` | 核心类型定义：`Request`（执行输入）、`Error`（失败描述）、`Stream`（事件流输出）、`Status`（活跃任务状态） |
| `configuration.py` | 配置聚合：把模型、密钥、人格、Skill、图片、上传文件、MCP 工具加载为一次可执行的 `ConfiguredRun` |
| `execution.py` | 执行层：用 `AsyncOpenAI` 手写流式循环与工具调用循环，产出渠道中性事件并持久化回复 |
| `service.py` | 服务层：`Service.run/status/cancel` 主流程与任务生命周期（`_run`）管理，含 `Dependencies` 依赖容器 |
| `session_lanes.py` | 会话级并发控制：保证同一用户同一会话同时只有一个任务在跑 |
| `user_stops.py` | 用户主动停止标记：记录哪些任务由用户取消，用于输出 `run.canceled` |

## 第二部分：架构设计

### ① 分层

本模块自底向上分为五层（HTTP 边界不在本模块内）：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | `Request` / `Error` / `Stream` / `Status`，模块对外契约 |
| 配置聚合层 | `configuration.py` | `RunConfigurations.load()` 把多个功能模块的配置聚合成 `ConfiguredRun` |
| 服务层 | `service.py` | `Service` 主流程：规范化 → 校验 → 会话准入 → 配置聚合 → 记录用量 → 启动执行；管理活跃任务表与生命周期 `_run` |
| 执行层 | `execution.py` | `chat()` 流式对话 + 工具调用循环，事件解码与用量统计 |
| 并发控制 / 状态辅助 | `session_lanes.py`、`user_stops.py` | 会话门闩、用户停止标记（`Service` 内部持有） |

HTTP 边界由 `webadapter.Adapter`（`internal/webadapter/adapter.py`）承担，它调用 `gateway.Service`，再落到本模块的 `Service`。本模块不持有任何 FastAPI Router，只暴露内部方法。

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（组合根装配） | `service.py: Dependencies`、`main.py: Service(Dependencies(...))` | 所有长期能力经 `Dependencies` 一次性注入；`RunConfigurations.__init__` 同样以构造注入方式接收各模块 |
| 适配器 | `webadapter/adapter.py`（HTTP/SSE → gateway）、`gateway/service.py`（渠道身份 → EDITH 身份） | 本模块是适配链的最内层，只认 EDITH 身份的 `Request` |
| 策略（可替换执行器） | `execution.py: chat()` 中 `tool_executor` 参数；`MCPToolContext` 与 `ToolRegistry` 均实现 `execute` | 工具执行按名分发：MCP 工具优先，否则交给注入的 `tool_executor` |
| 生产者-消费者 | `types.py: Stream`（`asyncio.Queue`）、`service.py: _run`（生产）、`webadapter/adapter.py`（消费） | `_run` 任务往队列写 `Event`，队列中的 `None` 表示流结束（对应 Go 版 channel 关闭） |
| 状态机（任务生命周期） | `service.py: _run` | `run.started` →（`message.delta` / `reasoning.delta` / `tool.started` / `tool.finished`）* → `run.completed` / `run.canceled` / `run.error` → `None` 哨兵 |
| 门闩（会话级互斥） | `session_lanes.py: SessionLanes` | `acquire/release`；asyncio 单线程事件循环下 `dict` 操作天然原子，无需显式加锁（见文件头注释） |
| 共享状态（单例持有） | `service.py: Service.active`（活跃任务表）、`user_stops.py: UserStops` | `Service` 生命周期内共享，跨请求查询/取消 |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Request` | `types.py` | 已完成后台身份与会话转换的执行输入（dataclass：request_id / user_id / session_id / message / image_ids / upload_paths / model_id / reasoning_option_id） |
| `Error` | `types.py` | 任务启动、执行或控制失败描述；`to_dict() -> {"type", "message"}` |
| `Stream` | `types.py` | 模块唯一输出；`events: asyncio.Queue[Event \| None]`，调用方必须消费到 `None` 哨兵 |
| `Status` | `types.py` | 活跃任务状态（request_id、status="running"） |
| `ConfiguredRun` | `configuration.py` | 可直接交给执行层的完整运行输入（request / definition / api_key / system_instruction / image_urls / message / mcp_tools） |
| `RunConfigurations` | `configuration.py` | 配置聚合器；`async load(request: Request) -> ConfiguredRun \| Error` |
| `message_with_uploads` | `configuration.py` | 把附件名称以 Markdown 形式追加进用户消息，保证会话历史可展示本次上传 |
| `run_instruction` / `skill_instruction` | `configuration.py` | 把公共/用户 Skill 摘要与上传文件路径拼成系统指令里的 Instruction 段落 |
| `Dependencies` | `service.py` | AgentRun 的依赖容器（dataclass：models / settings / providers / store / usage / skills / images / files / mcp / tool_definitions / tool_executor） |
| `ActiveRun` | `service.py` | 一个仍在执行的活跃任务（user_id / session_id / task: asyncio.Task） |
| `Service` | `service.py` | `async run(request) -> (Stream \| None, Error \| None)`；`status(user_id, request_id)`；`cancel(user_id, request_id)`；`async _run(...)` 生命周期 |
| `_normalize_request` / `_validate_request` | `service.py` | 请求字段 trim 规范化；必填校验（requestId/userId/sessionId 必填；message/imageIds/uploadPaths 至少一项） |
| `_summary_to_session_usage` | `service.py` | `usage.Summary` → `agentstream.SessionUsage` |
| `SessionLanes` | `session_lanes.py` | `acquire(user_id, session_id, request_id) -> bool`；`release(...)`；同一 `(user_id, session_id)` 只允许一个活跃任务 |
| `UserStops` | `user_stops.py` | `mark / contains / take(request_id)`；`take` 读取并清除标记 |
| `chat` | `execution.py` | `async chat(configured, store, queue, assistant_id, tools=None, tool_executor=None) -> Tokens`；流式对话 + 工具调用循环（上限 `MAX_TOOL_ROUNDS = 8`），回复持久化到会话存储 |
| `build_messages` | `execution.py` | 组装 OpenAI 消息列表：系统指令 + 历史 + 当前用户消息 |
| `_extract_reasoning` | `execution.py` | 从 chunk delta 提取推理增量，兼容不同供应商字段位置（`reasoning_content` / `model_extra`） |
| `tokens_from_usage` / `_add_tokens` | `execution.py` | OpenAI `CompletionUsage` → `usage.Tokens`；多轮用量累加（cached 为空则保持 None） |
| `MAX_TOOL_ROUNDS` | `execution.py` | 模块常量 = 8，工具调用轮次上限 |

## 第三部分：与其他模块的联系

### ① 依赖关系（import 了哪些模块 / SDK 及用途）

| 依赖 | import 位置 | 用途 |
| --- | --- | --- |
| `agentstream.types`（`Event` / `APIError` / `SessionUsage`） | `types.py`、`service.py` | 渠道中性的输出事件模型 |
| `agentstream.decoder`（`StreamDecoder`） | `execution.py` | 把内容增量/工具调用转为 `Event`（维护块 ID 状态） |
| `models.catalog`（`Catalog` / `DEFAULT_MODEL_ID` / `ModelDefinition`） | `configuration.py`、`service.py` | 按模型 ID 解析定义（base_url / api_model / vision / extra_fields） |
| `userconfig.settings`（`Settings`） | `configuration.py`、`service.py` | 读取用户默认模型与人格提示词 |
| `userconfig.providers`（`Providers`） | `configuration.py`、`service.py` | 按 provider_id 加载用户 API Key（缺失抛 `LookupError`） |
| `userconfig.mcp_runtime`（`MCPRuntime` / `MCPToolContext`） | `configuration.py` | 打开用户启用的 MCP 服务工具（每次 run 一个连接上下文） |
| `session.store`（`SessionStore`） | `execution.py`、`service.py` | 消息持久化：写用户消息、写 assistant 回复、读历史 |
| `usage.recorder`（`Recorder` / `RunAlreadyExists`）与 `usage.types`（`Run` / `Summary`） | `service.py` | 用量记录：start / finish / fail 与会话用量汇总 |
| `skills.catalog`（`Catalog`）+ `skills.types`（`SkillSummary` / `SYSTEM_PATH` / `CUSTOM_PATH`） | `configuration.py` | 公共 Skill 摘要与用户 overview，拼进系统指令 |
| `images.agent_input`（`AgentInput`） | `configuration.py`（经 `Dependencies.images` 注入） | 把图片 ID 转为本次调用可访问的短期 URL |
| `sandbox.agent_input`（`AgentInput`） | `configuration.py`（经 `Dependencies.files` 注入） | 校验上传文件只能引用当前用户当前会话 `uploads/` 内的普通文件 |
| `tools`（`ToolRegistry`） | `main.py` 装配后经 `Dependencies.tool_definitions` / `tool_executor` 注入 | 静态工具（系统工具 / Sandbox 工具 / Cron 工具）的定义与执行分发 |
| 外部 SDK：`openai`（`AsyncOpenAI`、`CompletionUsage`） | `execution.py` | OpenAI 兼容接口的流式 chat completions 调用 |
| 标准库：`asyncio`、`json`、`posixpath`、`dataclasses` | 各文件 | 队列/任务、工具参数解析、路径处理、类型定义 |

### ② 数据流向（一次典型业务流转）

```
前端 / Web BFF
   │  POST /internal/gateway/messages:stream (MessageRequest)
   ▼
webadapter.Adapter.stream_message            (webadapter/adapter.py)
   │  IncomingMessage(channel="web", ...)
   ▼
gateway.Service.run                          (gateway/service.py)
   │  身份翻译：WEB/CRON 为可信渠道直接用 userId；
   │  其余渠道经 bindings.to_clerk_user_id 换绑（未绑定 → identity_not_bound）
   │  to_agentrun_request(...) → agentrun.Request
   ▼
agentrun.Service.run                         (service.py)
   │  1. _normalize_request / _validate_request        → 失败返回 Error("invalid_request")
   │  2. lanes.acquire(user, session)                  → 占用中返回 Error("session_busy")
   │  3. configurations.load(request)                  → 模型解析/密钥/人格/上传文件/图片/Skill/MCP
   │  4. usage.start(Run)                              → 重复 requestId 抛 RunAlreadyExists → Error("request_conflict")
   │  5. store.add_user_message(...)                   → 持久化用户消息（含图片引用）
   │  6. 创建 asyncio.Queue 与 _run 后台任务，登记 active
   └─► 返回 Stream(events=queue)
   ▼
agentrun.Service._run                        (service.py, 后台任务)
   │  run.started
   │  └─► execution.chat(...)                (execution.py)
   │        · build_messages(系统指令 + store.get_messages 历史 + 用户消息)
   │        · AsyncOpenAI 流式请求（含图片则用户消息转多模态 content）
   │        · 逐 chunk 产出 message.delta / reasoning.delta
   │        · 有 tool_calls：tool.started → 执行（MCP 优先，否则 tool_executor）→ tool.finished → 回传结果继续，最多 8 轮
   │        · store.add_assistant_message(...) 持久化最终回复
   │  usage.finish(run, tokens) → run.completed（携带 sessionUsage）
   │  （用户取消 → run.canceled；异常 → run.error；CancelledError → run.canceled）
   │  finally：mcp_tools.close() → lanes.release → active.pop → queue.put_nowait(None)
   ▼
webadapter 消费队列                          (webadapter/adapter.py)
   │  data: {camelCase JSON 事件}\n\n（by_alias=True 输出 assistantId/blockId/toolCallId...）
   │  收到 None 后关闭 SSE 流
   ▼
前端
```

### ③ 接口调用

**本模块暴露的内部方法（无 HTTP 路由）**：

| 方法 | 签名 | 被谁调用 |
| --- | --- | --- |
| `Service.run` | `async run(request: Request) -> tuple[Stream \| None, Error \| None]` | `gateway.Service.run`（`gateway/service.py`） |
| `Service.status` | `status(user_id: str, request_id: str) -> tuple[Status \| None, Error \| None]` | `gateway.Service.status` |
| `Service.cancel` | `cancel(user_id: str, request_id: str) -> Error \| None` | `gateway.Service.cancel` |

模块出口（`__init__.py`）：`Dependencies`、`Service`、`Error`、`Request`、`Status`、`Stream`。

**HTTP 路由**（本模块不直接暴露，由 `webadapter.Adapter` 在 `main.py` 装配）：

| Method + Path | 说明 |
| --- | --- |
| `POST /internal/gateway/messages:stream` | 启动一次流式对话，SSE 返回事件流 |
| `GET /internal/gateway/runs/{request_id}` | 查询任务状态（query 参数 `userId`） |
| `POST /internal/gateway/runs/{request_id}/cancel` | 取消任务（query 参数 `userId`） |

**调用方链**：`main.py` 组合根创建 `agent_runs = Service(Dependencies(...))` → 传给 `new_gateway_service(users.bindings, agent_runs)` → `Adapter(gateway)`（Web）与 `CronAdapter(gateway)`（定时任务，`cronjob` 模块）均通过 gateway 触发 agentrun。

**涉及的 DB 表**：本模块不直接建表/写 SQL，经由依赖访问——`session.store` 写 `chat_messages` 表（`user_id / session_id / role / content / reasoning / image_ids`），`usage.recorder` 写 `usage_runs` 表（`request_id` 主键、token 用量、`status`：running/completed/failed）。

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **唯一执行入口与主流程**（`service.py: Service.run`）：规范化 → 校验 → 会话准入 → 配置聚合 → 记录用量 → 启动流式执行；启动失败统一返回 `(None, Error)`，调用方（webadapter）负责转 HTTP 错误。
2. **会话级串行**（`session_lanes.py`）：同一 `(user_id, session_id)` 同一时间只允许一个任务，抢占失败返回 `session_busy`；所有出口（错误、完成、取消）都必须 `release`。
3. **配置聚合顺序与降级**（`configuration.py: RunConfigurations.load`）：模型解析优先级为「请求指定 → 用户默认模型 → `DEFAULT_MODEL_ID`」；图片输入要求模型 `vision=True`；密钥缺失、上传文件校验失败、图片加载失败均统一转为 `Error("invalid_request", ...)`；Skill/MCP 为可选依赖，未注入则跳过。
4. **手写流式执行与工具循环**（`execution.py: chat`）：不使用 trpc-agent-go（Go 版方案 A），直接 `AsyncOpenAI` 流式调用（方案 B）；`StreamDecoder` 把增量编码为带块 ID 的中性事件；工具调用按 index 聚合参数、最多 8 轮（`MAX_TOOL_ROUNDS`），MCP 工具名优先匹配，其次 `tool_executor`；达到轮数上限仍持续调用工具时按正常结束处理并持久化已生成内容。
5. **生命周期事件与收尾**（`service.py: _run`）：`run.started` → 内容/工具事件 → `run.completed`（携带会话用量汇总）/ `run.canceled`（用户主动取消时）/ `run.error`（异常，`APIError(type="internal_error")`）；`finally` 中关闭 MCP 连接、释放会话门闩、从 `active` 移除、发送 `None` 哨兵，保证调用方不会悬挂。
6. **用量记录**（`usage.recorder`）：`start` 幂等防重（重复 requestId → `request_conflict`）；`finish` 写 token 用量并返回会话汇总（含 cache 命中率）；`fail` 标记失败运行且不计入会话汇总。
7. **身份与消息归一化**：`_normalize_request` 对所有字符串字段 trim；用户消息在配置聚合阶段追加附件清单（`message_with_uploads`），并在执行前先持久化（`add_user_message`）供会话历史回放。

### ② 配置项

**无**。本模块代码内未读取任何环境变量或配置项（没有 `os.getenv` / `environ` 调用）。与运行相关的环境变量（如 `EDITH_E2B_TEMPLATE`、`EDITH_COS_*`）由组合根 `main.py` 读取并注入到 Sandbox / Images 模块，再以依赖对象形式传入。本模块的"配置"全部来自：注入的依赖（`Dependencies`）、用户设置（`userconfig.Settings`：人格、默认模型）、供应商密钥（`userconfig.Providers`）、模型目录定义（`models.catalog.ModelDefinition`：`base_url` / `api_model` / `vision` / `extra_fields`）、`MAX_TOOL_ROUNDS`（代码常量）。

### ③ 异常处理

| 异常 / 场景 | 处理策略 | 代码位置 |
| --- | --- | --- |
| 启动期参数校验失败、模型不支持、密钥缺失、文件/图片加载失败 | 返回 `Error("invalid_request", ...)`，由 webadapter 映射为 **400** | `service.py: _validate_request`、`configuration.py: load` |
| 会话被占用 | 返回 `Error("session_busy", ...)` → **409** | `service.py: run` |
| 重复 requestId | 捕获 `usage.RunAlreadyExists` → `Error("request_conflict", ...)` → **409** | `service.py: run` |
| 查询/取消不存在的或不属于该用户的任务 | `Error("not_found", ...)` → **404** | `service.py: status/cancel` |
| 渠道用户未绑定 EDITH 身份 | gateway 返回 `identity_not_bound` → **403** | `gateway/service.py`（webadapter 映射） |
| 执行期任意异常 | `_run` 顶层捕获 `Exception` → `usage.fail` + 发 `run.error`（`APIError(type="internal_error")`） | `service.py: _run` |
| 执行期被取消（用户 cancel） | 捕获 `CancelledError` → `usage.fail` + 发 `run.canceled` 后重新抛出；`cancel()` 先 `user_stops.mark` 再 `task.cancel()`，使正常路径输出 `run.canceled` 而非 `run.completed` | `service.py: cancel/_run`、`user_stops.py` |
| 工具参数 JSON 解析失败 | 容错为空 dict `{}` | `execution.py: chat` |
| MCP 单个服务连接失败 | `mcp_runtime` 内记录 warning 并跳过该服务（降级，不影响其他服务与整体运行） | `userconfig/mcp_runtime.py`（依赖方） |

错误类型汇总（`Error.type`）：`invalid_request`、`session_busy`、`request_conflict`、`not_found`（本模块产生）；`identity_not_bound`、`internal_error`（gateway / 执行期事件产生）。HTTP 映射集中在 `webadapter/adapter.py: _gateway_error_to_http`。

### ④ 扩展点

| 扩展方向 | 做法 |
| --- | --- |
| 新增 Agent 工具 | 在 `main.py` 的 `ToolRegistry([...])` 中追加工具集（如 `SystemToolSet`、`SandboxToolSet`、`cron_jobs.tools`），实现 `tool_definitions()` 与 `execute(name, arguments, user_id, session_id)`；agentrun 自动合并其定义与执行，无需改本模块 |
| 新增 MCP 服务 / 工具 | 在 `userconfig.mcp` 中登记服务（sse / streamable 传输），执行层自动通过 `MCPToolContext` 打开并优先分发 |
| 新增模型 / 供应商 | 在 `models.catalog.model_definitions()` 增项（id / provider_id / api_model / base_url / vision / extra_fields），配置聚合层自动解析 |
| 新增渠道（如 IM、定时任务） | 在 `gateway.Service.run` 增加渠道身份翻译分支，并在对应 Adapter 增加路由；agentrun 无感知（cron 渠道已有先例：`CronAdapter(gateway)`） |
| 新增事件类型 / 字段 | 在 `agentstream.types.Event` 加字段、`agentstream.decoder.StreamDecoder` 加生成方法，执行层按需产出 |
| 调整工具调用轮次上限 | 修改 `execution.py` 的 `MAX_TOOL_ROUNDS` |
| 更换会话 / 用量存储 | `session.store.SessionStore`、`usage.recorder.Recorder` 背后替换实现即可，agentrun 面向接口使用 |

## 值得注意的实现细节

- 执行层为方案 B（文件头注释）：不依赖 Go 版的 trpc-agent-go，直接用 `AsyncOpenAI` 流式调用并手写工具调用循环，行为上对齐 Go 版 `execution.go + stream.go` 的框架执行部分。
- 事件输出使用 `by_alias=True` 序列化为 camelCase（`assistantId` / `blockId` / `toolCallId` / `sessionUsage`），与 Go 版 json tag 及前端契约保持一致（见 `webadapter/adapter.py`、`agentstream/types.py`）。
- `Service.active` 是进程内内存表，仅用于运行中任务的状态查询与取消；`status` 目前只返回固定值 `"running"`，历史任务状态请查询 `usage_runs` 表。
