# agentstream 模块

## 一、模块名称与简介

**模块名**：`edith_backend.internal.agentstream`

**一句话定位**：渠道中性事件模型与流式解码器——定义 EDITH 向所有渠道输出的唯一事件格式（`Event`），并把一次 Agent 回复的内容增量与工具调用翻译为这些事件。

**对应 Go 版源码路径**：`EDITH/backend-v2/internal/agentstream/`（`types.go`、`decoder.go`、`decoder_test.go`），模块职责一一对应。

**文件清单**：

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块门面：统一导出 `APIError`、`Event`、`SessionUsage`、`StreamDecoder`，声明与 Go 版对齐 |
| `types.py` | Pydantic v2 渠道中性事件模型：`APIError`、`SessionUsage`、`Event`（camelCase 序列化） |
| `decoder.py` | `StreamDecoder` 流式解码器：维护块 ID 状态，把内容增量与工具调用转为中性事件 |

## 二、架构设计

### ① 分层

本模块位于 `src/edith_backend/internal/` 内层，是**纯类型层 + 状态机层**，不包含 HTTP 边界、服务层或数据访问层：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | 纯 Pydantic 数据模型，无任何行为；定义事件及其子结构 |
| 解码/状态机层 | `decoder.py` | `StreamDecoder` 维护一次回复的块顺序状态，产出 `Event` 实例 |

模块不直接与数据库、HTTP、外部 SDK 交互，属于被上层（`agentrun`、`gateway`、`webadapter`、`cronadapter`）引用的叶子模块。

### ② 设计模式

| 模式 | 位置 | 说明 |
| --- | --- | --- |
| 事件模型（渠道中性事件） | `types.py` 中 `Event`/`SessionUsage`/`APIError` | 统一的输出契约：所有渠道（Web/SSE、Cron）都只消费 `Event`，与具体供应商/框架解耦 |
| 状态机（块 ID 分配） | `decoder.py` 中 `StreamDecoder` 的 `next_block_id`/`last_type`/`last_id`/`started_tools` | 同一类型连续增量共享一个 `block_id`，类型切换时分配新块；`end_content_block()` 重置状态，`tool.*` 事件会强制结束当前内容块 |
| 工厂方法（按需构造事件） | `decoder.py` 中 `content_delta`/`tool_started`/`tool_finished` | 三个方法把调用方的内容/工具增量包装为带正确 `block_id`/`tool_status` 的 `Event` |
| 门面导出 | `__init__.py` | 对外只暴露 4 个符号，隐藏内部实现细节 |
| 序列化适配（别名策略） | `types.py` 中 `ConfigDict(alias_generator=to_camel, populate_by_name=True)` | 输出 camelCase JSON（`assistantId`/`blockId`/`toolCallId`…），对齐 Go 版 json tag；构造时仍可用 snake_case 或 camelCase 传参 |

注：模块自身**不**使用依赖注入/仓储/适配器/策略（无外部依赖需要注入）；但它本身作为依赖被 `main.py` 显式装配进 `agentrun` 的 `Dependencies`。

### ③ 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `APIError` | `types.py` | 描述一次任务启动或执行错误：`type: str`、`message: str` |
| `SessionUsage` | `types.py` | 一次会话累计用量的渠道中性格式：`total_tokens`、`cached_prompt_tokens`、`uncached_prompt_tokens`、`completion_tokens`、`cache_hit_rate`，以及覆盖度 `total_runs` / `cache_reported_runs` / `estimated_runs`。缓存字段缺失时 SSE 走 `exclude_none` 会整个省略该 key，消费方必须按「没有就是没有」处理 |
| `Event` | `types.py` | 唯一事件格式，14 个字段；`usage` 字段显式序列化为 `sessionUsage`（`Field(default=None, alias="sessionUsage")`） |
| `StreamDecoder` | `decoder.py` | 保存一次 Agent 回复中的块顺序，输出渠道中性事件；构造签名 `__init__(self, assistant_id: str)` |
| `StreamDecoder.content_delta(block_type, event_type, delta) -> Event` | `decoder.py` | 生成 `reasoning.delta` 或 `message.delta`；同类型连续增量共享 `block_id`，类型切换分配新块 |
| `StreamDecoder.tool_started(tool_call_id, tool_name, arguments) -> Event` | `decoder.py` | 生成 `tool.started` 事件（`tool_status="running"`），并结束当前内容块；`tool_call_id` 记入 `started_tools` |
| `StreamDecoder.tool_finished(tool_call_id, tool_status, tool_result) -> Event` | `decoder.py` | 生成 `tool.finished` 事件，并结束当前内容块 |
| `StreamDecoder.end_content_block() -> None` | `decoder.py` | 清空 `last_type`/`last_id`，强制后续增量开启新块 |

## 三、与其他模块的联系

### ① 依赖关系

- **内部模块**：无。`decoder.py` 仅 `from .types import Event`，`types.py` 不 import 任何内部模块。
- **外部依赖**：仅 `pydantic`（`BaseModel`、`ConfigDict`、`Field`、`alias_generators.to_camel`）。
- 对比：Go 版 `types.go`/`decoder.go` 依赖 `trpc.group/trpc-go/trpc-agent-go`（`model`、`event` 包）；Python 版不依赖任何 agent 框架 SDK，直接由 `agentrun/execution.py` 手写流式循环调用本模块方法。

**本模块被谁使用**（经 `main.py` 组合根显式装配）：

| 使用方 | 用法 |
| --- | --- |
| `agentrun/execution.py` | `StreamDecoder(assistant_id)` 实例化；`content_delta("reasoning", "reasoning.delta", …)`、`content_delta("text", "message.delta", …)`、`tool_started(...)`、`tool_finished(...)` 产生事件放入队列 |
| `agentrun/service.py` | 直接构造 `Event`（`run.started`/`run.completed`/`run.canceled`/`run.error`）；`_summary_to_session_usage()` 把 `usage.Summary` 转为 `SessionUsage`；`run.error` 携带 `APIError(type="internal_error", …)` |
| `agentrun/types.py` | `Stream.events: asyncio.Queue[Event \| None]`，`None` 为流结束哨兵 |
| `webadapter/adapter.py` | 逐个消费事件，`event.model_dump_json(exclude_none=True, by_alias=True)` 输出 camelCase SSE 行 |
| `cronadapter/adapter.py` | 消费事件直到 `None`，检查 `event.type == "run.error"` 并抛错 |

### ② 数据流向

一次典型业务流转（Web 渠道流式对话）：

```
webadapter POST /internal/gateway/messages:stream
  └─ gateway.Service.run()            # 渠道身份 → EDITH 身份（webadapter/adapter.py:47）
       └─ agentrun.Service.run()      # 会话准入/配置聚合/记录用量（agentrun/service.py:72）
            ├─ 创建 asyncio.Queue[Event|None]（service.py:106）
            ├─ 队列放入 run.started（service.py:152）
            ├─ execution.chat() 流式循环：
            │    ├─ StreamDecoder.content_delta → reasoning.delta / message.delta
            │    ├─ StreamDecoder.tool_started → tool.started（执行工具）
            │    └─ StreamDecoder.tool_finished → tool.finished（回传结果继续循环）
            ├─ 队列放入 run.completed / run.canceled / run.error（携带 SessionUsage / APIError）
            └─ 队列放入 None 哨兵（service.py:206）
  ├─ webadapter 侧：逐个 Event → model_dump_json(by_alias=True) → SSE "data: …"（adapter.py:63-70）
  └─ cronadapter 侧：消费事件，遇 run.error 抛 RuntimeError（cronadapter/adapter.py:34-42）
```

数据进出本模块的形态：**进**——`execution.chat` 的增量字符串与工具调用信息（经 `StreamDecoder` 方法）；**出**——`Event` 实例（进入 `asyncio.Queue`），以及 `webadapter` 侧序列化后的 camelCase JSON（SSE 行）。

### ③ 接口调用

- **HTTP 路由**：无。本模块不注册任何路由，也不读取请求。
- **暴露的内部方法**：

| 方法 | 调用方 |
| --- | --- |
| `StreamDecoder(assistant_id)` | `agentrun/execution.py:109` |
| `StreamDecoder.content_delta(...)` | `agentrun/execution.py:133,136` |
| `StreamDecoder.tool_started(...)` | `agentrun/execution.py:161` |
| `StreamDecoder.tool_finished(...)` | `agentrun/execution.py:182` |
| 类型 `Event`/`SessionUsage`/`APIError` | `agentrun/service.py`、`agentrun/types.py`、`webadapter/adapter.py` |
| 序列化 `Event.model_dump_json(exclude_none=True, by_alias=True)` | `webadapter/adapter.py:70`（输出 SSE） |

- 生命周期事件（`run.started`/`run.completed`/`run.canceled`/`run.error`）不由本模块生成，由 `agentrun/service.py` 直接构造 `Event`。

## 四、模块内容扩展说明

### ① 核心业务逻辑要点

1. **三类事件**：
   - 内容增量：`reasoning.delta`（推理）、`message.delta`（正文），由 `content_delta` 生成，`block_type` 分别为 `"reasoning"` / `"text"`；
   - 工具事件：`tool.started`（`tool_status` 固定 `"running"`）、`tool.finished`（`tool_status` 由调用方传入，`execution.py` 目前固定传 `"completed"`）；
   - 生命周期事件：`run.started` / `run.completed` / `run.canceled` / `run.error`，由 `agentrun` 直接构造。
2. **块 ID 分配**：`block_id` 格式为 `{assistant_id}_{block_type}_{序号}`；同类型连续增量共享同一块，`tool.*` 事件或类型切换会结束当前内容块（`end_content_block` 清空 `last_type`/`last_id`），下一段增量获得新块。
3. **实例隔离**：每个事件流必须使用独立 `StreamDecoder` 实例（`decoder.py` 文档字符串明确约定），状态不跨流共享。
4. **序列化约定**：全部 camelCase（`alias_generator=to_camel` + `populate_by_name=True`）；`usage` 字段显式 `alias="sessionUsage"`，对齐 Go 版 `json:"sessionUsage,omitempty"`，前端 `RunCompletedEvent`/`RunCanceledEvent` 按 `sessionUsage` 读取；`Event` 共 14 个字段（`type`、`session_id`、`request_id`、`assistant_id`、`block_id`、`block_type`、`delta`、`tool_call_id`、`tool_name`、`arguments`、`tool_status`、`tool_result`、`usage`、`error`）。
5. **与 Go 版的差异**：Go 版 `Decoder.DecodeFrameworkEvent`（依赖 trpc-agent-go 框架事件、含 `startedTools` 去重与 `sawPartialReasoning`/`sawPartialText` 兜底）在 Python 版中**未实现**；Python 版只提供 `content_delta`/`tool_started`/`tool_finished` 三个原子方法，由 `agentrun/execution.py` 的手写流式循环直接调用（方案 B：不依赖 trpc-agent-go）。`started_tools` 集合目前仅记录，未用于去重判断（去重由调用方流式结构保证）。

### ② 配置项

**无**。本模块不读取任何环境变量或配置（`main.py` 中的 `EDITH_E2B_TEMPLATE`、`EDITH_COS_*` 分别属于 sandbox、images 模块）。

### ③ 异常处理

- 本模块**无异常处理逻辑**（无 `try/except`、无错误降级、不记录日志）。
- 事件层面的错误由调用方处理：`agentrun/service.py` 的 `_run` 顶层捕获任意异常，转为 `Event(type="run.error", error=APIError(type="internal_error", message=str(exc)))` 放入队列；`cronadapter` 消费到 `run.error` 时抛 `RuntimeError`；`webadapter` 则在启动失败时把 `agentrun.Error` 映射为 HTTP 状态码（400/403/404/409/500）。

### ④ 扩展点

- **新增块类型**：`content_delta` 接受任意 `block_type` 字符串，传入新值（如 `"reasoning"`/`"text"` 之外的类型）即自动获得独立 `block_id` 序列，无需改代码。
- **新增事件类型/字段**：在 `Event` 中追加可选字段（保持 camelCase 别名约定），由 `agentrun` 直接构造对应 `type` 的事件即可；前端按字段名读取。
- **新增工具状态**：`tool_finished` 的 `tool_status` 为调用方入参，可传 `"completed"`/`"failed"` 之外的任意状态。
- **对齐 Go 框架事件解码**：若未来需要复刻 `DecodeFrameworkEvent` 的框架事件 → 中性事件解释层（含工具去重、partial 兜底），可在 `decoder.py` 中新增入口方法，不影响现有三个方法。
- **测试**：Go 版存在 `decoder_test.go`，Python 版当前未发现对应测试文件，可参考 Go 测试用例为 `StreamDecoder` 补充单元测试。

## 摘要

- 撰写了 `agentstream/README.md`，覆盖四大部分：模块简介与文件清单、架构设计（类型层+状态机层、事件模型/状态机/工厂/门面/别名适配模式、关键类函数表）、模块联系（零内部依赖、被 agentrun/webadapter/cronadapter 经 gateway 使用、SSE 数据流向、无 HTTP 路由）、扩展说明（块 ID 机制、无配置、无自身异常处理、四类扩展点）。
- 值得注意：① `__init__.py` 文档字符串称 Event 为 13 个字段，实际计数为 14 个（含 `usage`/`error`）；② Python 版砍掉了 Go 版 `DecodeFrameworkEvent` 解释层与 `startedTools` 去重，改为手写流式循环直调三个原子方法；③ 本模块无配置、无异常处理、无 HTTP 路由，是纯叶子模块；④ Go 版有 `decoder_test.go`，Python 版未发现对应测试。
