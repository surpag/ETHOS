# cronjob 模块 README

## 第一部分：模块名称与简介

**模块名**：`cronjob`（Python 路径 `src/edith_backend/internal/cronjob/`）

**一句话定位**：用户定时任务（cron job）的存储、HTTP 管理、Agent 工具调用与后台调度执行能力。

**对应 Go 版源码路径**：`EDITH/backend-v2/internal/cronjob/`（文件一一对应，职责对齐，本文档描述以 Python 实现为准）。

**文件清单**：

| 文件名 | 一句话职责 |
| --- | --- |
| `__init__.py` | 包导出入口：暴露 `HTTP`、`Module`、`Scheduler`、`Job`、`JobInput`、`create_schema`、`new_module` |
| `types.py` | 领域类型与 DTO 定义：`Job`/`JobInput` 数据类、Pydantic 请求/响应模型、`InvalidJob`/`JobNotFound` 异常、任务类型常量 |
| `schema.py` | 建表函数 `create_schema`：创建 `cron_jobs` 表 |
| `store.py` | SQLite 仓储（Repository）：任务 CRUD 与调度抢占相关 SQL 访问 |
| `schedule_rules.py` | 调度规则：任务输入校验（`validate_input`）与下次执行时间计算（`next_run`） |
| `scheduler.py` | 后台调度器：周期轮询到点任务、原子抢占、异步派发执行 |
| `tools.py` | Agent 工具集 `CronToolSet`：`create_cron_job` 工具定义与执行 |
| `http.py` | HTTP 管理接口：`/internal/cron-jobs*` 路由与错误映射 |
| `module.py` | 模块组装入口：`new_module` 工厂、`Module.new_scheduler` 调度器工厂 |

---

## 第二部分：架构设计

### ① 分层

按代码实际分层，自上而下：

| 层次 | 载体 | 说明 |
| --- | --- | --- |
| HTTP 边界层 | `http.py`（`HTTP` 类） | FastAPI `APIRouter`，定义 `/internal/cron-jobs*` 路由；负责参数校验、调用 `Store`、把领域异常映射为 `EDITHError`、将 `Job` 转为 `JobResponse` |
| 服务/调度层 | `scheduler.py`（`Scheduler` 类） | 后台循环：扫描到点任务、抢占、异步执行、收尾；不触碰 SQL，全部经由 `Store` |
| 领域规则层 | `schedule_rules.py` | 纯函数校验与时间计算（cron/一次性时间解析、时区处理），被 `Store` 调用 |
| 数据访问层 | `store.py`（`Store` 类） | 对 `aiosqlite.Connection` 的全部 SQL 封装（CRUD + 调度 SQL），即仓储实现 |
| 类型定义层 | `types.py`、`schema.py` | 领域数据类/异常/Pydantic DTO（`types.py`）与表结构 DDL（`schema.py`） |
| 组装层 | `module.py`（`Module`） | 由 `main.py` 调用 `new_module(db, settings)` 一次性装配 store/http/tools，并提供 `new_scheduler(runner)` |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `HTTP.__init__(store, settings)`、`Store.__init__(db, settings)`、`Scheduler.__init__(store, runner)`、`CronToolSet.__init__(store)` | 依赖全部通过构造器显式注入；`main.py` 作为组合根完成装配 |
| 工厂函数 | `module.py: new_module(db, settings)`、`Module.new_scheduler(runner)` | 异步工厂组装模块；`new_scheduler` 每次按注入的 runner 创建调度器 |
| 仓储（Repository） | `store.py` 的 `Store` 类 | 所有 SQL 收敛于此，业务层不直接接触 SQL；对外暴露语义化方法（`create/list/update/delete/set_enabled/due_jobs/claim_due/finish_run/initialize_next_runs`） |
| 适配器（Adapter） | 外部模块 `cronadapter.Adapter`（`main.py` 中 `CronAdapter(gateway)`） | 实现 `JobRunner` 协议，把 `Job` 转成 Gateway 消息（`CRON_CHANNEL`），是 cronjob 与 gateway/agentrun 之间的适配器 |
| 协议/结构化子类型（Protocol） | `scheduler.py: class JobRunner(Protocol)` | 用 `typing.Protocol` 声明 `run_job(job: Job) -> None` 契约，运行时鸭子类型匹配，无需继承 |
| DTO/视图模型 | `types.py: JobResponse` | 面向 Web 的安全表示，裁剪内部字段（如不暴露 `clerk_user_id`），时间字段序列化为字符串 |
| 异常分层映射 | `http.py` + `http_utils.EDITHError` | 领域异常（`InvalidJob`/`JobNotFound`）在 HTTP 层转为统一的 `{"type","message"}` JSON 错误 |
| 状态标记位（轻量状态机） | `cron_jobs` 表的 `enabled`/`running` 列 | 任务的启用/禁用（toggle）、运行中抢占、执行后释放由布尔列驱动，配合 `next_run_at` 表达调度状态 |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Job` | `types.py` | 任务领域数据类：`id, clerk_user_id, name, task_type, schedule, prompt, enabled=True, next_run_at=None, running=False, created_at=None` |
| `JobInput` | `types.py` | 创建/编辑时允许填写的字段：`name, task_type, schedule, prompt` |
| `CreateRequest` | `types.py` | 创建/更新任务的 HTTP 输入（camelCase 别名、`extra="forbid"`）：`user_id, name, task_type, schedule, prompt, timezone`；`userId` 由 BFF 注入 |
| `JobResponse` / `ListResponse` | `types.py` | 任务响应模型（`next_run_at`/`created_at` 为 ISO 字符串）与任务列表响应 |
| `InvalidJob` / `JobNotFound` | `types.py` | 领域异常：输入/调度校验失败、任务不存在 |
| `TASK_TYPE_ONCE` / `TASK_TYPE_RECURRING` | `types.py` | 任务类型常量：`"once"` / `"recurring"` |
| `create_schema(db)` | `schema.py` | `async` 建表：`CREATE TABLE IF NOT EXISTS cron_jobs (...)`，含 `enabled`/`running` 整数列 |
| `Store` | `store.py` | 仓储类：`create(user_id, input_)`、`list(user_id)`、`update(user_id, job_id, input_)`、`delete(user_id, job_id)`、`set_enabled(user_id, job_id, enabled)`、`due_jobs(now)`、`claim_due(job_id, now)`、`finish_run(job_id, now)`、`initialize_next_runs(now)` |
| `validate_input(input_)` | `schedule_rules.py` | 校验 name/schedule/prompt 非空、task_type ∈ {once, recurring}，失败抛 `InvalidJob` |
| `next_run(settings, user_id, input_, from_time)` | `schedule_rules.py` | `async` 计算下次执行时间：once 解析 RFC3339；recurring 按用户时区（默认 `Asia/Shanghai`）+ `croniter` 计算 |
| `Scheduler` | `scheduler.py` | 调度器：`run()` 启动无限轮询（间隔 `DEFAULT_POLL_INTERVAL = 10` 秒），`_schedule_due_jobs()` 扫描+抢占，`_run_claimed(job)` 执行并收尾 |
| `JobRunner` | `scheduler.py` | `Protocol`：`async def run_job(self, job: Job) -> None`，由 `cronadapter.Adapter` 实现 |
| `CronToolSet` | `tools.py` | 工具集：`tool_definitions()` 返回 `create_cron_job` 定义；`execute(name, arguments, user_id, session_id)` 创建任务并返回 JSON |
| `HTTP` | `http.py` | HTTP 管理类：构造时构建 `self.router`（`APIRouter`），内含 5 个路由 |
| `Module` / `new_module` | `module.py` | 模块容器（`store`/`http`/`tools` 三件套）；`new_module(db, settings)` 为异步工厂 |

---

## 第三部分：与其他模块的联系

### ① 依赖关系

**内部模块（`src/edith_backend/internal/`）**：

| 被依赖方 | 使用方 | 用途 |
| --- | --- | --- |
| `userconfig.settings.Settings` | `http.py`、`store.py`、`module.py` | 用户时区读写：`load_timezone`（调度计算用）、`save_timezone`（创建任务时随 `timezone` 字段保存） |
| `http_utils.EDITHError` | `http.py` | 统一业务错误，映射为 `{"type","message"}` JSON 响应 |
| `cronadapter.Adapter` | `main.py` 注入（cronjob 模块自身不 import） | 作为 `JobRunner` 注入 `new_scheduler`，把任务转 Gateway 消息执行；其 `adapter.py` 反向 import 了 `cronjob.types.Job` 与 `gateway.service/type` |

**外部 SDK / 标准库**：

| 依赖 | 用途 |
| --- | --- |
| `aiosqlite` | 异步 SQLite 访问（`schema.py`、`store.py`、`module.py`） |
| `pydantic`（`BaseModel`/`ConfigDict`/`alias_generators.to_camel`） | HTTP DTO 定义与 camelCase 别名（`types.py`） |
| `croniter` | cron 表达式解析与下次执行时间计算（`schedule_rules.py`） |
| `zoneinfo.ZoneInfo` | 时区定位（`schedule_rules.py`） |
| `fastapi.APIRouter` | 路由注册（`http.py`） |
| 标准库 `asyncio`、`logging`、`datetime`、`uuid`、`dataclasses` | 后台任务、日志、时间、ID 生成、数据类 |

### ② 数据流向

**场景 A：用户创建任务（HTTP 入口）**

```
BFF → POST /internal/cron-jobs（CreateRequest，userId 由 BFF 注入）
  → HTTP.create_job
      ├─ 若 body.timezone 非空 → Settings.save_timezone(user_id, timezone)   # 写 userconfig 时区
      └─ Store.create(user_id, JobInput(...))
            ├─ schedule_rules.validate_input(...)                             # 校验失败抛 InvalidJob
            ├─ schedule_rules.next_run(settings, user_id, input_, now)        # 算 next_run_at
            └─ INSERT INTO cron_jobs (... enabled=1, running=0 ...)           # 写 DB
  → JobResponse（camelCase JSON 返回 BFF/Web）
```

**场景 B：调度执行（后台流转）**

```
main.py 启动 → cron_scheduler = cron_jobs.new_scheduler(CronAdapter(gateway))
  → asyncio.create_task(cron_scheduler.run())
Scheduler.run：
  ├─ 启动时 Store.initialize_next_runs(now)          # 补齐 enabled=1 且 next_run_at 为空的记录
  └─ 每 10s 循环 Store.due_jobs(now)                 # 查询 enabled=1, running=0, next_run_at<=now
       └─ 逐个 Store.claim_due(job_id, now)          # 原子 UPDATE running=1；rowcount!=1 则跳过（防重复）
            └─ asyncio.create_task(_run_claimed(job))
                 ├─ runner.run_job(job)              # cronadapter.Adapter → Gateway
                 │    ├─ gateway.run(IncomingMessage(channel="cron", external_user_id=job.clerk_user_id,
                 │    │     session_key=f"cron:{job.id}", message=job.prompt))
                 │    └─ 消费事件流直到结束/run.error（执行结果落在该 Agent 会话历史中）
                 └─ Store.finish_run(job.id, now)    # running=0；recurring 重算 next_run_at，once 置 enabled=0
```

**场景 C：Agent 工具调用（工具入口）**

```
Agent 会话 → ToolRegistry.execute("create_cron_job", arguments, user_id, session_id)
  → CronToolSet.execute → Store.create(...) → 返回 JSON（含 id、nextRunAt、message="定时任务已创建"）
```

### ③ 接口调用

**HTTP 路由（`HTTP.router`，由 `main.py` 的 `app.include_router(cron_jobs.http.router)` 挂载）**：

| Method | Path | 入参 | 出参/状态码 |
| --- | --- | --- | --- |
| GET | `/internal/cron-jobs` | query `userId` | `ListResponse`；userId 为空 → 400 `invalid_request` |
| POST | `/internal/cron-jobs` | body `CreateRequest`（含 `timezone`） | `JobResponse`；非法任务 → 400 `invalid_cron_job` |
| PUT | `/internal/cron-jobs/{job_id}` | body `CreateRequest` | `JobResponse`；400 / 404 `cron_job_not_found` |
| DELETE | `/internal/cron-jobs/{job_id}` | query `userId` | 204；404 `cron_job_not_found` |
| POST | `/internal/cron-jobs/{job_id}/toggle` | query `userId`、`enabled`（"true"/"false"） | `JobResponse`；400 / 404 |

**内部方法/接口（被谁调用）**：

| 接口 | 调用方 |
| --- | --- |
| `Module.store` / `Module.http` / `Module.tools` | `main.py` 组合根读取并装配 |
| `Module.new_scheduler(runner)` | `main.py`：`cron_jobs.new_scheduler(CronAdapter(gateway))` |
| `Scheduler.run()` | `main.py`：`asyncio.create_task(cron_scheduler.run())`，进程退出时 `scheduler_task.cancel()` |
| `CronToolSet.tool_definitions()` / `execute()` | `main.py`：注册进 `ToolRegistry([SystemToolSet(), sandbox_tools, cron_jobs.tools])`，由 agentrun 经 `tool_executor=tools` 分发调用 |
| `JobRunner.run_job(job)`（协议） | `Scheduler._run_claimed`；实现方为 `cronadapter.Adapter` |
| `Store.*` | `HTTP`、`Scheduler`、`CronToolSet` |
| `Settings.load_timezone/save_timezone` | `Store`、`HTTP` |
| `create_schema(db)` | `main.py` lifespan 启动建表 |

---

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **任务模型**：任务归属用户（`clerk_user_id`），由 `name/task_type/schedule/prompt` 定义；`task_type` 仅支持 `once`（RFC3339 绝对时间）与 `recurring`（五段 cron 表达式）。
2. **调度时间计算**：`schedule_rules.next_run` 按用户时区计算——once 直接解析 `datetime.fromisoformat`，recurring 用 `croniter` 从 `from_time` 取下一触发时刻；用户未设置时区时回退默认 `Asia/Shanghai`。
3. **原子抢占（防重复执行）**：`claim_due` 用条件 `UPDATE ... SET running=1 WHERE id=? AND enabled=1 AND running=0 AND next_run_at<=?`，以 `rowcount != 1` 判断抢占失败并跳过——即使部署多实例也不会重复派发同一任务。
4. **执行生命周期**：抢占成功即 `asyncio.create_task` 异步执行（不阻塞轮询循环）；执行后无论成败都会 `finish_run` 释放 `running` 标记；recurring 任务重算下一次时间，once 任务执行完自动 `enabled=0`（一次性）。
5. **启动自愈**：调度器启动时 `initialize_next_runs` 为「已启用但 `next_run_at` 为空」的历史任务补齐下次执行时间。
6. **执行渠道解耦**：调度器只依赖 `JobRunner` 协议；真实执行交给 `cronadapter.Adapter`，其把任务包装成 `channel="cron"`、`session_key=f"cron:{job.id}"` 的 Gateway 消息，因此任务执行结果会写入对应 Agent 会话历史。
7. **工具与 HTTP 共用仓储**：`create_cron_job` 工具与 POST 路由最终都走 `Store.create`，校验规则一致；工具层把异常吞成 `{"error": ...}` JSON 返回给 Agent。

### ② 配置项

**环境变量**：无。cronjob 模块内未读取任何环境变量（`EDITH_E2B_TEMPLATE` 等由 `main.py` 中其他模块如 sandbox 读取，与本模块无关）；数据库连接由 `main.py` 的 `config.database_path()` 统一提供。

**代码内常量（非环境配置）**：

| 常量 | 位置 | 值 | 说明 |
| --- | --- | --- | --- |
| `_DEFAULT_TIMEZONE` | `schedule_rules.py` | `"Asia/Shanghai"` | 用户未配置时区时的默认时区 |
| `DEFAULT_POLL_INTERVAL` | `scheduler.py` | `10` | 调度轮询间隔（秒） |
| `cron_jobs` | `schema.py` | 表名 | 任务表：`id, clerk_user_id, name, task_type, schedule, prompt, enabled, next_run_at, running, created_at` |

### ③ 异常处理

| 异常 | 抛出位置 | 处理策略 |
| --- | --- | --- |
| `InvalidJob` | `schedule_rules`（name/schedule/prompt 为空、task_type 非法、once 非 RFC3339、时区未知、cron 表达式非法）；`store.create`（user_id 为空） | HTTP 层捕获 → `EDITHError(400, "invalid_cron_job", msg)`；工具层捕获 → JSON `{"error": msg}` 返回 Agent |
| `JobNotFound` | `store.update/delete/set_enabled`（按 id+user 查无记录） | HTTP 层捕获 → `EDITHError(404, "cron_job_not_found", "定时任务不存在")` |
| `EDITHError` | `http.py`（userId 缺失 → 400 `invalid_request`） | 由 `main.py` 注册的全局异常处理器转为 `{"type","message"}` JSON 响应（`http_utils.register_error_handler`） |
| `RequestValidationError` | FastAPI 校验（含 `extra="forbid"` 未知字段） | 全局处理器统一 400 `invalid_request` |
| 调度器内各类 `Exception` | `Scheduler.run/_schedule_due_jobs/_run_claimed/finish_run` | 宽捕获并 `logger.warning`，**不中断轮询循环**（降级策略：单轮失败不影响后续扫描）；任务执行异常后仍会执行 `finish_run` 释放占用标记 |
| 工具执行内各类 `Exception` | `CronToolSet.execute` | 宽捕获 → JSON `{"error": str(exc)}` 返回给 Agent 可见 |

### ④ 扩展点

1. **新增 Agent 工具**：在 `tools.py` 的 `tool_definition()` 追加定义、`CronToolSet.tool_definitions()` 自动携带、`execute()` 增加对应 name 分支（复用 `Store` 即可）。
2. **新增 HTTP 路由**：在 `http.py` 的 `_build_router()` 内添加路由装饰函数，挂载无需改动（`main.py` 已 include 整个 router）。
3. **新增任务类型/调度规则**：在 `types.py` 增加 `TASK_TYPE_*` 常量，扩展 `schedule_rules.py` 的 `validate_input`（类型白名单）与 `next_run`（新类型的下次时间计算）；注意 `store.finish_run` 中「once 执行完置 enabled=0」的分支需同步设计。
4. **更换执行渠道**：实现 `JobRunner` 协议（`async def run_job(job: Job) -> None`）的新 runner（参考 `cronadapter.Adapter`），在 `main.py` 中装配并传给 `Module.new_scheduler(runner)` 即可，调度器与仓储零改动。
5. **更换存储实现**：保持 `Store` 公开方法签名不变，替换 aiosqlite 实现（如换成其他数据库/ORM），上层（HTTP/Scheduler/Tools）无需改动。
6. **时区策略调整**：默认时区与用户时区加载均集中在 `schedule_rules._DEFAULT_TIMEZONE` 与 `Settings.load_timezone`，可在此两处调整。

---

> 说明：本 README 内容全部依据 `backend-py/src/edith_backend/internal/cronjob/` 现有代码与 `main.py` 组合根核对编写；未发现的内容（如环境变量读取）已如实标注。
