# gateway（多渠道统一入口）模块说明

# 第一部分：模块名称与简介

## 模块名与定位

**gateway** —— 所有 Agent 渠道（web / cron / 未来新渠道）的统一入口（门面）：把「渠道身份 + 会话」翻译为「EDITH 身份 + 会话」，再交给 `agentrun.Service` 启动、查询或取消 AgentRun。

- Python 实现路径：`src/edith_backend/internal/gateway/`
- 对应 Go 版源码路径：`internal/gateway/`（`backend-v2/internal/gateway/types.go`、`service.go`），模块职责一一对应
- 装配位置：由组合根 `src/edith_backend/main.py` 显式创建，被 `webadapter`（Web 渠道）与 `cronadapter`（cron 渠道）调用；本模块自身**不注册任何 HTTP 路由**（HTTP 路由注册在 webadapter）

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `types.py` | 类型定义：渠道常量、`IncomingMessage` 渠道事实、`to_agentrun_request` 翻译函数 |
| `service.py` | Gateway 服务：身份/会话翻译 + 启动/查询/取消 AgentRun 的转发门面 |
| `__init__.py` | 模块导出：`IncomingMessage`、`Service`、`new_service` |

# 第二部分：架构设计

## ① 分层

本模块只有两层，HTTP 边界与数据访问均位于外部模块：

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | `IncomingMessage` 数据类、`WEB_CHANNEL`/`CRON_CHANNEL` 常量、`to_agentrun_request()` 翻译函数 |
| 服务层（门面） | `service.py` `Service` | 校验渠道输入 → 翻译身份 → 转发 `agent_runs.run/status/cancel`；`new_service()` 工厂函数负责装配校验 |

- **HTTP 边界**：不在本模块，而在 `webadapter/adapter.py`（构造 `IncomingMessage` 并消费事件流）。
- **数据访问层**：不在本模块，渠道绑定查询在 `userconfig/bindings.py`（读 `user_channel_bindings` 表）。

## ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 门面（Facade） | `service.py` `Service` | "所有 Agent 渠道的唯一入口"，屏蔽 `agentrun` 执行与 `userconfig` 绑定的复杂度 |
| 依赖注入（构造器注入） | `service.py` `Service.__init__(self, bindings, agent_runs)` | 依赖以参数注入，不在模块内创建 |
| 工厂函数 | `service.py` `new_service(bindings, agent_runs)` | 校验依赖非空（为空抛 `ValueError`），返回 `Service`；main.py 通过它装配 |
| 适配器（由调用方实现） | `webadapter/adapter.py`、`cronadapter/adapter.py` | 外部协议（HTTP/SSE、cron 调度）各自适配为 gateway 调用，gateway 自身不感知协议 |
| 信任渠道策略（分支） | `service.py` `_is_trusted_channel(channel)` | web/cron 直接信任 `external_user_id`；其余渠道必须走身份绑定翻译 |
| DTO 翻译 | `types.py` `to_agentrun_request()` | 渠道事实 `IncomingMessage` → AgentRun 运行输入 `agentrun.Request` |

## ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `WEB_CHANNEL`、`CRON_CHANNEL` | `types.py` | 渠道常量：`"web"`、`"cron"`（可信渠道） |
| `IncomingMessage` | `types.py` | `@dataclass`：Adapter 交给 Gateway 的渠道事实（channel、external_user_id、session_key、request_id、message、image_ids、upload_paths、model_id、reasoning_option_id） |
| `to_agentrun_request(msg, user_id, session_id)` | `types.py` | 把渠道消息翻译为 `agentrun.Request`（字段一一映射，`request_id`/`message`/`image_ids`/`upload_paths`/`model_id`/`reasoning_option_id` 透传） |
| `Service` | `service.py` | Gateway 门面；构造器注入 `bindings: Bindings` 与 `agent_runs: AgentRunService` |
| `Service.run(input_msg: IncomingMessage)` | `service.py` | `async def run(...) -> tuple[Stream \| None, Error \| None]`：规范化 → 校验 → 身份翻译 → 转发 `agent_runs.run` |
| `Service.status(user_id, request_id)` | `service.py` | `-> tuple[Status \| None, Error \| None]`：转发查询属于该用户的活跃任务 |
| `Service.cancel(user_id, request_id)` | `service.py` | `-> Error \| None`：转发取消属于该用户的活跃任务 |
| `new_service(bindings, agent_runs)` | `service.py` | 工厂函数；任一依赖为 `None` 抛 `ValueError("gateway requires bindings and agent runs")` |
| `_is_trusted_channel(channel)` | `service.py` | `-> bool`：`channel in (WEB_CHANNEL, CRON_CHANNEL)` |

模块对外导出（`__init__.py`）：`IncomingMessage`、`Service`、`new_service`。

# 第三部分：与其他模块的联系

## ① 依赖关系

| 依赖 | 用途 |
| --- | --- |
| `..agentrun.service`（`Service as AgentRunService`） | 启动（`run`）、查询（`status`）、取消（`cancel`）AgentRun 的唯一执行入口 |
| `..agentrun.types`（`Error`、`Status`、`Stream`、`Request`） | AgentRun 的结果/错误/事件流类型；`to_agentrun_request` 产出 `Request` |
| `..userconfig.bindings`（`Bindings`） | 渠道身份 → Clerk 用户翻译（`to_clerk_user_id`，查 `user_channel_bindings` 表，未绑定 `found=False`） |
| 外部 SDK | 无（仅标准库 `dataclasses` 与 `from __future__ import annotations`） |

## ② 数据流向

**Web 渠道（可信，直接信任身份）**：

```
Web BFF ──POST /internal/gateway/messages:stream──▶ webadapter.Adapter
  （body 携带 Clerk 注入的 userId/sessionId）
  ──IncomingMessage(channel="web", external_user_id=userId, session_key=sessionId, ...)──▶ gateway.Service.run
  ──（可信渠道：user_id=external_user_id，session_id=session_key）──▶ to_agentrun_request
  ──agentrun.Request──▶ agent_runs.run ──▶ Stream(events 队列)
  ◀── SSE 事件流（data: 事件 JSON，by_alias=True 输出 camelCase）── webadapter 逐事件 yield 回 BFF
```

**cron 渠道（可信，身份来自任务）**：

```
cronjob 调度器 ──▶ cronadapter.Adapter.run_job(job)
  ──IncomingMessage(channel="cron", external_user_id=job.clerk_user_id, session_key=f"cron:{job.id}", request_id=uuid4().hex, message=job.prompt)──▶ gateway.Service.run
  ──▶ agent_runs.run ──▶ 消费事件直到收到 None 哨兵；遇 run.error 抛 RuntimeError
```

**未来非可信渠道（如微信/Telegram）**：

```
新 Adapter ──IncomingMessage(channel="wechat", external_user_id=渠道账号)──▶ gateway.Service.run
  ──bindings.to_clerk_user_id(channel, external_user_id) 查 user_channel_bindings──▶
    未绑定 ──▶ Error("identity_not_bound")；已绑定 ──▶ user_id=Clerk 用户，session_id=f"{channel}:{user_id}"
  ──▶ agent_runs.run
```

## ③ 接口调用

**HTTP 路由（注册在 webadapter，非本模块）**：

| Method | Path | 调用的 gateway 方法 |
| --- | --- | --- |
| POST | `/internal/gateway/messages:stream` | `Service.run` |
| GET | `/internal/gateway/runs/{request_id}`（query：`userId`） | `Service.status` |
| POST | `/internal/gateway/runs/{request_id}/cancel`（query：`userId`） | `Service.cancel` |

**内部方法调用方**：

| 调用方 | 调用点 |
| --- | --- |
| `webadapter.adapter.Adapter` | `self.gateway.run/status/cancel`（路由处理器内） |
| `cronadapter.adapter.Adapter` | `run_job` 中 `self._gateway.run(...)` |
| `main.py`（组合根） | `new_gateway_service(users.bindings, agent_runs)` 创建；随后 `Adapter(gateway)`、`CronAdapter(gateway)`、`cron_jobs.new_scheduler(cron_adapter)` |

# 第四部分：模块内容扩展说明

## ① 核心业务逻辑要点

1. **输入规范化**：`run` 先对 `channel`、`external_user_id`、`session_key` 做 `strip()`；channel 或 external_user_id 为空 → `Error("invalid_request", "channel and externalUserId are required")`。
2. **可信渠道判定**：`_is_trusted_channel` 认定 `web`/`cron` 为可信渠道，直接使用传入身份（`user_id = external_user_id`，`session_id = session_key`）；此时若 `session_key` 为空 → `Error("invalid_request", "sessionKey is required for trusted channels")`。
3. **非可信渠道身份翻译**：调用 `bindings.to_clerk_user_id`；`ValueError`（入参为空）→ `Error("internal_error", str(exc))`；未绑定 → `Error("identity_not_bound", ...)`；绑定成功 → `user_id = bound_user_id or ""`（空值兜底），`session_id = f"{channel}:{user_id}"`。
4. **转发执行**：通过 `to_agentrun_request` 翻译为 `agentrun.Request` 后调用 `agent_runs.run`；`status`/`cancel` 为纯转发（agentrun 侧校验 userId/requestId 非空、校验任务归属，未命中返回 `not_found`）。
5. **无状态**：本模块不持有任务状态，活跃任务（`active` 字典、会话车道 `SessionLanes`、用户停止标记）全部在 `agentrun.Service` 内管理。

## ② 配置项

**无**。gateway 模块自身不读取任何环境变量或配置项。注意区分：`main.py` 中读取的 `EDITH_E2B_TEMPLATE`（默认 `"default"`）属于 sandbox 模块、`EDITH_COS_BUCKET/REGION/SECRET_ID/SECRET_KEY` 属于 images 模块，与 gateway 无关。

## ③ 异常处理

| 场景 | 处理策略 | 说明 |
| --- | --- | --- |
| `bindings.to_clerk_user_id` 抛 `ValueError` | 捕获并降级为返回 `Error("internal_error", str(exc))` | `service.py` `run()` 内 try/except，不向上抛 |
| 依赖为空 | `new_service` 抛 `ValueError` | 装配期错误，由组合根 main.py 的组装失败暴露 |
| 业务失败 | 一律以 `(result, Error)` 返回值表达，不抛异常 | `Error.type` 有 `invalid_request` / `internal_error` / `identity_not_bound`（gateway 自身产出），其余透传 agentrun 的错误（`session_busy`、`request_conflict`、`not_found` 等） |
| 调用方映射 | 由 adapter 负责 | `webadapter._gateway_error_to_http`：`invalid_request`→400、`session_busy`/`request_conflict`→409、`identity_not_bound`→403、`not_found`→404、其他→500；`cronadapter` 将 `run_error` 包装为 `RuntimeError`，执行期 `run.error` 事件同样转为 `RuntimeError` |
| 日志 | 未发现本模块内的日志调用 | 错误经由返回值/HTTP 错误响应暴露（待确认是否有全局日志兜底，见 `http_utils`） |

## ④ 扩展点

- **新增渠道**：定义渠道常量（如 `WECHAT_CHANNEL = "wechat"`，放入 `types.py`），并新增一个 Adapter 模块（仿照 `webadapter`/`cronadapter`）构造 `IncomingMessage` 后调用 `gateway.run`。非可信渠道需先在 `user_channel_bindings` 表绑定渠道账号 ↔ Clerk 用户（`Bindings.bind`），否则返回 `identity_not_bound`。
- **新增 gateway 转发方法**：在 `service.py` 的 `Service` 中加方法代理 `agent_runs` 即可（如按 request_id 查询更细状态），并在 `__init__.py` 确认导出。
- **新增能力不在本模块扩展**：新工具在 `tools/ToolRegistry`（`SystemToolSet`/`SandboxToolSet`/cron 工具）、新 provider 在 `userconfig/providers`、新存储在各模块 schema——gateway 只负责把渠道输入送达 `agentrun`。
- **无本模块专属 DB 表**：渠道绑定数据属于 userconfig（`user_channel_bindings`），本模块通过 `Bindings` 只读访问。
