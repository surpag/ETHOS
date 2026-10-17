# cronadapter —— 定时任务到 AgentRun 的适配器

## 一、模块名称与简介

| 项目 | 内容 |
| --- | --- |
| 模块名 | `cronadapter`（包路径 `edith_backend.internal.cronadapter`） |
| 一句话定位 | 把定时任务（`cronjob.Job`）翻译成 Gateway 的 cron 渠道消息并启动、消费 AgentRun，是定时任务与 Agent 执行之间的渠道适配器。 |
| 对应 Go 版源码 | `backend-v2/internal/cronadapter/`（`adapter.go`、`README.md`） |

### 文件清单

| 文件 | 职责 |
| --- | --- |
| `adapter.py` | 定义 `Adapter` 类：`run_job()` 将已抢占的 `Job` 转为 `IncomingMessage`（`channel="cron"`）交给 Gateway，并消费事件流直到 AgentRun 收尾。 |
| `__init__.py` | 模块导出：`from .adapter import Adapter`，`__all__ = ["Adapter"]`。 |

## 二、架构设计

### 1. 分层

本模块非常薄，只包含「服务/适配」一层，不涉及 HTTP 边界与数据访问：

| 层 | 说明 | 代码位置 |
| --- | --- | --- |
| 类型定义 | 自身不定义业务类型，直接复用 `cronjob.types.Job`（输入）与 `gateway.types.IncomingMessage` / `CRON_CHANNEL`（输出） | `adapter.py` 顶部 import |
| 服务层（渠道适配） | `Adapter` 类：把 cron 任务事实翻译为 Gateway 消息并消费结果事件 | `adapter.py` 第 15–42 行 |
| HTTP 边界 | 无（本模块不注册任何路由；HTTP 由 cronjob 模块暴露） | — |
| 数据访问层 | 无（不直接读写数据库；任务存储、到点扫描与抢占由 `cronjob.store` 负责） | — |

### 2. 设计模式

| 模式 | 说明 | 代码位置 |
| --- | --- | --- |
| 适配器模式（Adapter） | 模块即适配器：把 cron 渠道的 `Job` 适配为 Gateway 的 `IncomingMessage`（`channel="cron"`），对调用方隐藏 AgentRun 的启动与事件消费细节 | `adapter.py` 的 `Adapter.run_job` |
| 依赖注入（构造器注入） | `Adapter.__init__(gateway)` 由组合根 `main.py` 显式注入 `gateway` 实例（`main.py` 第 101 行 `CronAdapter(gateway)`），模块内部不自行创建 | `adapter.py` 第 18–19 行 |
| 接口隔离 / 协议（Protocol） | `cronjob/scheduler.py` 定义 `JobRunner` 协议（`async def run_job(self, job: Job) -> None`），`Adapter` 结构性满足该协议；调度器依赖协议而非具体类，可被任何实现 `run_job` 的对象替换 | `scheduler.py` 第 21–22 行；`adapter.py` 第 15 行 |
| 事件流 + 哨兵消费 | `agentrun.Stream` 用 `asyncio.Queue` + `None` 哨兵表示流结束（对应 Go 版 channel 关闭）；`run_job` 循环 `await stream.events.get()` 直到取到 `None` | `adapter.py` 第 34–40 行 |

### 3. 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Adapter` | `adapter.py` | Cron 渠道执行器；不负责轮询和任务存储。`__init__(self, gateway: GatewayService) -> None` |
| `Adapter.run_job` | `adapter.py` | `async def run_job(self, job: Job) -> None`：把一个已抢占任务交给 Gateway，并消费事件直到 AgentRun 收尾；失败时抛异常。 |
| （复用）`Job` | `cronjob/types.py` | 一条定时任务定义（dataclass），含 `id`、`clerk_user_id`、`task_type`、`prompt` 等字段。 |
| （复用）`IncomingMessage` / `CRON_CHANNEL` | `gateway/types.py` | 渠道消息 dataclass 与渠道常量 `CRON_CHANNEL = "cron"`。 |

## 三、与其他模块的联系

### 1. 依赖关系

| 依赖 | 用途 |
| --- | --- |
| `cronjob.types.Job`（内部模块） | 输入类型：`run_job` 接收的定时任务定义。 |
| `gateway.service.Service`（内部模块） | 核心依赖（构造器注入）：`gateway.run()` 以可信渠道方式启动 AgentRun。 |
| `gateway.types.CRON_CHANNEL`、`IncomingMessage`（内部模块） | 构造 cron 渠道消息。 |
| 标准库 `uuid.uuid4` | 每次运行生成独立 `request_id`（`request_id=uuid4().hex`）。 |
| 外部 SDK | 无（不 import 任何第三方 SDK）。 |

注：cronadapter 不 import cronjob 的 scheduler/store，只消费 `Job` 类型；反向由 scheduler 通过 `JobRunner` 协议调用本模块。

### 2. 数据流向

一次典型流转（按 `main.py` 装配）：

```text
cronjob.Scheduler.run（main.py 115 行 asyncio.create_task 启动的常驻循环）
   │ 每 10 秒（DEFAULT_POLL_INTERVAL）轮询 store.due_jobs() → store.claim_due() 原子抢占（cron_jobs 表 running=1）
   ▼
Scheduler._run_claimed(job) ──► JobRunner.run_job(job)      【调用点：scheduler.py 56 行】
   ▼
cronadapter.Adapter.run_job(job)
   │ 构造 IncomingMessage{
   │   channel="cron", external_user_id=job.clerk_user_id,
   │   session_key="cron:{job.id}", request_id=uuid4().hex, message=job.prompt }
   ▼
gateway.Service.run(msg)          【cron 属于可信渠道，跳过用户绑定查找】
   │ 直接以 external_user_id 为 user_id、session_key 为 session_id
   ▼
agentrun.Service.run(Request) → AgentRun 执行
   ▼
Stream.events（asyncio.Queue<Event|None>）
   ▲
run_job 循环消费直到 None 哨兵；run.error 事件 → 抛 RuntimeError
   ▼
Scheduler._run_claimed 捕获异常并记 warning 日志
   ▼
store.finish_run(job.id)：recurring 重算 next_run_at；once 置 enabled=0（cron_jobs 表）
```

要点：

- `Job` 数据来自 `cron_jobs` 表（`cronjob/store.py`），但本模块不直接访问该表；
- cron 渠道在 gateway 中属于可信渠道（`gateway/service.py` 的 `_is_trusted_channel` 对 `"web"`、`"cron"` 返回 True），因此不做用户绑定查找，身份直接取 `job.clerk_user_id`；
- 执行结果沉淀在 `cron:{job.id}` 对应的 Agent 会话历史中（`Job` docstring 注明）。

### 3. 接口调用

本模块**不暴露任何 HTTP 路由**（无 router；定时任务的 HTTP 接口由 cronjob 模块的 `HTTP` 暴露，`main.py` 110 行注册）。

对外提供的方法：

| 方法 | 被谁调用 |
| --- | --- |
| `Adapter.run_job(job)` | `cronjob.Scheduler._run_claimed`（`scheduler.py` 56 行）——scheduler 由 `main.py` 102 行 `cron_jobs.new_scheduler(cron_adapter)` 创建，115 行启动其 `run()` 循环 |

本模块调用外部：

| 调用 | 说明 |
| --- | --- |
| `gateway.run(IncomingMessage)` | 启动 AgentRun 并取得事件流（`gateway/service.py` 25 行） |
| `stream.events.get()` | 消费事件队列直到 `None` 哨兵（`agentrun/types.py` 的 `Stream`） |

装配位置（`main.py`）：同一 `gateway` 实例同时注入 webadapter（100 行）与 cronadapter（101 行），两者是平级的渠道适配器。

## 四、模块内容扩展说明

### 1. 核心业务逻辑要点

- **渠道翻译**：把 cron 任务（`Job`）翻译为 Gateway 的 cron 渠道 `IncomingMessage`，每次运行生成独立 `request_id`（`uuid4().hex`）。
- **会话隔离**：`session_key = "cron:{job.id}"`，同一任务的每次运行进入同一 Agent 会话，结果写入该会话历史。
- **可信渠道**：cron 是受信渠道，绕过 `userconfig.bindings` 的用户映射，直接用 `job.clerk_user_id` 作为用户身份。
- **事件消费**：`while True: event = await stream.events.get(); if event is None: break`——必须消费到 `None` 哨兵才算收尾（对齐 Go 版 channel 关闭语义）。
- **错误提升**：只关注 `run.error` 事件；启动失败（`gateway.run` 返回的 `run_error`）与执行失败（`run.error` 事件）都转为 `RuntimeError` 向上抛出。
- **与 Go 版的差异**：Go `RunJob(ctx, job)` 返回 error，Python `run_job(job)` 直接抛异常（且无 context 参数）；Go `New(agentGateway)` 工厂校验 gateway 非空并返回 error，Python 构造器不做非空校验。

### 2. 配置项

无。本模块不读取任何环境变量或配置（`EDITH_E2B_TEMPLATE`、`EDITH_COS_*` 等由 `main.py` 读取并注入 sandbox、images 等其他模块）。

### 3. 异常处理

| 异常 | 触发位置 | 处理策略 |
| --- | --- | --- |
| `RuntimeError("start cron agent run: {run_error.message}")` | `run_job` 中 `gateway.run` 返回的 `run_error` 非 None | 立即抛出（启动失败） |
| `RuntimeError(event.error.message)` | 事件流中出现 `type == "run.error"` 且 `event.error` 非 None | 记入 `execution_error`，循环结束后抛出 |
| 上述异常向上传播 | — | 由 `cronjob.Scheduler._run_claimed` 捕获并 `logger.warning("run cron job %s: %s", job.id, exc)`（`scheduler.py` 57 行），随后照常 `finish_run` 收尾，调度循环不受影响 |

本模块自身不打日志、不做 HTTP 错误映射（无 HTTP 面）；异常处理依赖调用方（scheduler）兜底。

### 4. 扩展点

- **新增渠道**：仿照本模块与 `webadapter`，新写一个 Adapter 组装 `IncomingMessage` 后统一走 `gateway.run`（gateway 是所有渠道的唯一入口）。
- **替换执行器**：scheduler 依赖 `JobRunner` 协议，任何实现了 `async run_job(job)` 的对象都可替换 `Adapter`（例如换执行通道或模拟执行），无需改动 scheduler。
- **增强结果处理**：可在事件消费循环中增加对其他事件类型的处理（如成功/工具事件），或将 `request_id` 透出，以便调用 `gateway.status(user_id, request_id)` / `gateway.cancel(...)` 管理运行。
- **对齐 Go 版校验**：如需与 Go `New` 工厂行为一致，可在构造器或装配处补充 gateway 非空校验。
