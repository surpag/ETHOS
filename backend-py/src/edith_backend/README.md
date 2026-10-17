# edith_backend 包根：组合根与共享基础设施

## 第一部分：包结构总览

**一句话定位**：`edith_backend` 是 EDITH 的 Python 后端包（对 Go 版 `backend-v2` 的 1:1 重构），本目录即包根，承载**进程入口（组合根）**与**跨模块共享基础设施**（配置加载、数据库连接、统一错误契约、工具聚合器）；16 个纵向业务模块全部位于 `internal/` 下。

**源码路径**：`backend-py/src/edith_backend/`；对应 Go 版 `backend-v2/cmd/server/`（入口/配置/数据库）+ `backend-v2/internal/tools/`（工具聚合）+ `backend-v2/internal/httpx/`（统一错误）。

**顶层文件清单**：

| 文件 | 一句话职责 |
| --- | --- |
| `main.py` | 进程入口与组合根：`lifespan` 里打开数据库、创建全部模块、显式装配依赖、注册路由；`main()` 以 uvicorn（reload）启动 |
| `config.py` | 环境变量加载与配置读取：`EDITH_DB_PATH` / `EDITH_ADDR` / `EDITH_E2B_TEMPLATE` 等 |
| `database.py` | SQLite 连接管理：单连接、`row_factory=Row`、DELETE 日志模式、忙等待超时 |
| `__init__.py` | 包文档注释 |
| `internal/__init__.py` | `internal` 包文档注释 |
| `internal/http_utils.py` | 统一错误契约：`EDITHError` 异常 + 全局异常处理器（`{"type","message"}` JSON） |
| `internal/tools.py` | 工具聚合器 `ToolRegistry`：汇总各模块工具集，按名分发执行 |

**模块地图**（`internal/` 下 16 个模块，均有一一对应的 Go 版与独立 README）：

| 模块 | 对应 Go 版 | 一句话职责 |
| --- | --- | --- |
| `agentrun` | `internal/agentrun` | Agent 唯一执行入口：配置聚合、流式对话、工具循环、生命周期 |
| `agentstream` | `internal/agentstream` | 渠道中性事件模型与流式解码器（块 ID 状态） |
| `conversation` | `internal/conversation` | 会话历史读取与 Timeline 投影 |
| `cronadapter` | `internal/cronadapter` | 定时任务到 AgentRun 的渠道适配器 |
| `cronjob` | `internal/cronjob` | 定时任务管理、调度、工具与 HTTP |
| `gateway` | `internal/gateway` | 多渠道统一入口与身份翻译 |
| `images` | `internal/images` | 图片上传/预览（腾讯云 COS 签名 URL） |
| `models` | `internal/models` | 模型目录与供应商定义 |
| `sandbox` | `internal/sandbox` | E2B 沙箱生命周期、文件接口与工具 |
| `session` | `internal/session` | 会话消息持久化（`chat_messages` 表） |
| `skills` | `internal/skills` | 技能目录与 HTTP |
| `systemtools` | `internal/systemtools` | 系统内置工具集 |
| `usage` | `internal/usage` | 用量记录与汇总 |
| `userconfig` | `internal/userconfig` | 用户设置、供应商密钥、渠道绑定、MCP |
| `volume` | `internal/volume` | 用户 Skills Volume 绑定 |
| `webadapter` | `internal/webadapter` | Web 渠道 HTTP/SSE 适配器（Gateway 的 HTTP 边界） |

## 第二部分：架构设计

### ① 分层

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 进程入口层 | `main.py`（`main()` + `create_app()`） | uvicorn 启动 `edith_backend.main:app`（开发模式 `reload=True`） |
| 组合根/装配层 | `main.py` 的 `lifespan` | 启动时按固定顺序创建 DB → 建表 → 各模块 → 显式注入依赖 → 注册路由；退出时回收资源 |
| 配置层 | `config.py` | 从 `.env` / 进程环境读取配置（utf-8/gbk 容错加载） |
| 数据访问层 | `database.py` | 唯一 SQLite 连接，注入各模块 |
| 共享基础设施 | `internal/http_utils.py`、`internal/tools.py` | 统一错误处理、工具注册表 |
| 业务模块层 | `internal/*`（16 个模块） | 纵向功能模块，各自带类型/业务/HTTP 边界 |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 组合根（Composition Root） | `main.py` `lifespan` | 全部模块在此创建并以构造器注入互连；模块之间不互相 import 装配，避免循环依赖 |
| 依赖注入 | `main.py` 中对 `Service(Dependencies(...))`、`Adapter(gateway)`、`new_gateway_service(...)` 等的显式注入 | 各模块面向接口/对象协作 |
| 工厂函数 | `main.py` 中 `new_*`（如 `new_gateway_service`、`new_conversation_module`）及各模块 `module.py` | 创建并校验依赖后返回模块对象 |
| 统一错误处理（异常 → 响应契约） | `http_utils.register_error_handler` | `EDITHError` → `{"type","message"}`；`RequestValidationError` → 400 `invalid_request`（对齐 Go `ReadJSON` 的 DisallowUnknownFields） |
| 注册表（Registry） | `internal/tools.py` `ToolRegistry` | 汇总多个工具集，按 `function.name` 建立名字 → 工具集映射，统一 `tool_definitions()` / `execute()` |
| 单连接数据访问（写串行） | `database.py` | aiosqlite 单连接线程模型天然串行，对应 Go 版 `SetMaxOpenConns(1)` |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `load_environment` | `config.py` | `() -> None`；加载 `./.env` 与 `../.env`（进程环境优先），utf-8/gbk 编码回退；无法解码抛 `RuntimeError` |
| `database_path` | `config.py` | `() -> str`；`EDITH_DB_PATH`，默认 `"edith-v2.db"` |
| `runtime_address` | `config.py` | `() -> str`；`EDITH_ADDR`，默认 `"127.0.0.1:8080"` |
| `sandbox_template` | `config.py` | `() -> str`；`EDITH_E2B_TEMPLATE`，为空抛 `RuntimeError("EDITH_E2B_TEMPLATE 是必填配置")`。**注意：当前 `main.py` 未调用它**，而是 `os.getenv("EDITH_E2B_TEMPLATE", "default")` 兜底（见 sandbox 模块 README 的"待确认"） |
| `open_database` | `database.py` | `async (path: str) -> aiosqlite.Connection`；`row_factory=Row`；`PRAGMA journal_mode=DELETE`（不用 WAL，规避 Windows 沙箱 mmap 锁残留）；`PRAGMA busy_timeout=5000` |
| `lifespan` | `main.py` | FastAPI lifespan：装配全部模块（见下方数据流向）并 yield；退出时取消调度器、关闭 DB |
| `create_app` | `main.py` | `() -> FastAPI`；创建 app、注册全局错误处理器、挂 `/health` |
| `main` | `main.py` | `() -> None`；解析 `runtime_address()` 后 `uvicorn.run("edith_backend.main:app", reload=True)` |
| `EDITHError` | `internal/http_utils.py` | 业务错误异常：`(status_code, error_type, message)` 三元组（对应 Go `httpx.WriteError`） |
| `register_error_handler` | `internal/http_utils.py` | `(app) -> None`；注册 `EDITHError` 与 `RequestValidationError` 两个异常处理器 |
| `ToolRegistry` | `internal/tools.py` | `__init__(tool_sets)` 建名映射；`tool_definitions() -> list[dict]`；`async execute(name, arguments, user_id, session_id) -> str`（未知工具返回 `{"error": "unknown tool: ..."}` JSON） |

## 第三部分：与其他模块的联系

### ① 依赖关系

`main.py`（唯一依赖汇聚点）import 了全部 16 个业务模块与共享设施，按序装配：

```
open_database(config.database_path())
  → create_session_schema / create_volume_schema / create_sandbox_schema / create_cronjob_schema
  → new_userconfig_module(db, DEFAULT_MODEL_ID)          # users：设置/供应商/绑定/MCP
  → new_models_module(users.providers)                   # 模型目录（依赖 userconfig）
  → new_usage_module(db)
  → SessionStore(db)
  → new_conversation_module(session_store, usage.reader)
  → VolumeService(VolumeStore(db))
  → SandboxService(db, EDITH_E2B_TEMPLATE, volumes)
  → SandboxToolSet(sandboxes) / SandboxAgentInput(sandboxes)
  → SkillsCatalog(volumes)
  → new_cronjob_module(db, users.settings)
  → new_images_module(db, COSConfig(EDITH_COS_*))
  → ToolRegistry([SystemToolSet(), sandbox_tools, cron_jobs.tools])
  → MCPRuntime(users.mcp)
  → agentrun.Service(Dependencies(models/settings/providers/store/usage/skills/images/files/mcp/tool_definitions/tool_executor))
  → new_gateway_service(users.bindings, agent_runs)
  → Adapter(gateway)（webadapter，Web 渠道）
  → CronAdapter(gateway)（cron 渠道）
  → cron_jobs.new_scheduler(cron_adapter)
```

`config.py` / `database.py` 被 `main.py` 使用；`http_utils.py` 被所有暴露 HTTP 的模块使用（统一抛 `EDITHError`）；`tools.py` 被 `main.py` 装配并被 `agentrun` 消费（`tool_definitions` / `tool_executor`）。

### ② 数据流向（请求入口总览）

```
浏览器 / Web BFF（Clerk 注入 userId）
  ├─ POST /internal/gateway/messages:stream        → webadapter → gateway → agentrun → SSE 事件流
  ├─ GET/POST /internal/gateway/runs/{id}[/cancel] → webadapter → gateway → agentrun（状态/取消）
  ├─ GET /internal/conversations[/{session_id}]    → conversation → session.store + usage.reader
  ├─ GET/PUT /internal/user-settings, /internal/mcp-servers* → userconfig
  ├─ GET /internal/models, /internal/available-models → models → userconfig.providers
  ├─ GET /internal/skills                          → skills → volume（读 overview）
  ├─ /internal/sandbox/files*                      → sandbox → volume（挂载）→ E2B
  ├─ /internal/images*                             → images → 腾讯云 COS
  └─ /internal/cron-jobs*                          → cronjob（调度经 cronadapter → gateway → agentrun）
定时器：cron scheduler ──► CronAdapter ──► gateway ──► agentrun
```

所有渠道（Web / cron）最终都收敛到 `gateway → agentrun` 这一条执行链。

### ③ 接口调用

- **对外 HTTP 路由**：全部注册于 `main.py` 的 `app.include_router(...)`（users.http、MCP 路由、model_catalog.http、conversations.http、SkillsHTTP、SandboxHTTP、cron_jobs.http、images.http、web.router），路径前缀均为 `/internal/*`；另有 `/health` 健康检查。
- **共享工具接口**：`ToolRegistry.tool_definitions()` / `ToolRegistry.execute()` 由 `agentrun.execution.chat()` 消费；`ToolRegistry` 聚合 `SystemToolSet`、`SandboxToolSet`、`cron_jobs.tools` 三套工具。
- **共享错误接口**：任何模块 `raise EDITHError(status, type, message)` 即被全局处理器转为 `{"type","message"}` 响应，与 Go 版完全一致。

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **单进程单连接 SQLite**：`open_database` 只开一个 aiosqlite 连接并注入所有模块（`lifespan` 中创建、结束时 `close`）；显式 `journal_mode=DELETE` 不用 WAL，规避 Windows 沙箱下 `-shm`/`-wal` 锁残留与 safe-delete 拦截（`database.py` 文件头注释说明了原因）。
2. **建表集中在前置步骤**：`lifespan` 启动即创建 session / volume / sandbox / cronjob 四张 schema，之后模块才能安全使用 DB。
3. **模块创建顺序即依赖顺序**：先创建"被依赖方"（userconfig、usage、session、volume），再创建"依赖方"（sandbox、skills、images、cronjob），最后组装执行链（tools → agentrun → gateway → 渠道适配器）。
4. **工具三层聚合**：静态工具（系统工具 + 沙箱工具 + cron 工具）经 `ToolRegistry` 聚合后注入 agentrun；动态工具（用户启用的 MCP）由 `MCPRuntime` 按 run 打开，执行时 MCP 优先于静态工具（见 agentrun 模块 README）。
5. **统一错误契约**：业务错误一律 `EDITHError`；请求体校验失败（含未知字段，`extra="forbid"` 场景）统一 400 `invalid_request`，对齐 Go 版 `httpx.ReadJSON` 的严格校验语义。

### ② 配置项（全部环境变量）

| 环境变量 | 默认值 | 读取位置 | 用途 |
| --- | --- | --- | --- |
| `EDITH_DB_PATH` | `edith-v2.db` | `config.database_path()`（`main.py` 使用） | SQLite 文件路径 |
| `EDITH_ADDR` | `127.0.0.1:8080` | `config.runtime_address()`（`main.main()` 使用） | HTTP 监听地址 |
| `EDITH_E2B_TEMPLATE` | `"default"`（`main.py:66` 兜底；`config.sandbox_template()` 定义为必填但未被调用） | `main.py` | E2B Sandbox 模板名，注入 sandbox 模块 |
| `EDITH_COS_BUCKET / REGION / SECRET_ID / SECRET_KEY` | `""` | `main.py`（构造 `COSConfig`） | 腾讯云 COS，注入 images 模块 |
| `EDITH_E2B_DEBUG_HTTP` | 未设置 | `volume/trace.py` | 开启 E2B Volume 诊断日志（`"true"` 时） |
| `.env` 文件 | — | `config.load_environment()` | 根目录 `.env` 或 `backend-py/.env`，utf-8/gbk 容错 |

**未定义/未使用**：`config.sandbox_template()`（`config.py:51-56`）当前是死代码——`main.py` 未调用它（与 Go 版启动时 `log.Fatal` 的必填校验不一致，见 sandbox 模块 README 的"待确认"说明）。

### ③ 异常处理

| 异常/场景 | 处理策略 | 代码位置 |
| --- | --- | --- |
| 业务错误 `EDITHError(status, type, message)` | 全局处理器 → HTTP `status` + `{"type","message"}` | `http_utils.register_error_handler` |
| 请求体校验失败（含未知字段） | 全局处理器 → `400 {"type":"invalid_request","message":"invalid request"}` | `http_utils.register_error_handler` |
| `.env` 解码失败（utf-8/gbk 均失败） | `config.load_environment` 抛 `RuntimeError`，进程启动失败 | `config.py:_load_dotenv_tolerant` |
| DB 打开/PRAGMA 失败 | `open_database` 关闭连接后重新抛出 | `database.py` |
| 工具执行：未知工具名 | `ToolRegistry.execute` 返回 `{"error":"unknown tool: <name>"}` JSON 字符串（不抛异常） | `internal/tools.py` |

### ④ 扩展点

| 扩展方向 | 做法 |
| --- | --- |
| 新增业务模块 | 在 `internal/<new>/` 建包（类型 + 服务 + `module.py` 工厂 + 可选 `http.py`），在 `main.py` `lifespan` 中按依赖顺序创建并 `include_router`；遵循"模块间只通过组合根注入"的约束 |
| 新增 Agent 工具集 | 实现 `tool_definitions()` 与 `execute(name, arguments, user_id, session_id)`，加入 `main.py` 的 `ToolRegistry([...])` 即可被 agentrun 自动使用 |
| 新增 HTTP 路由 | 在对应模块的 `_build_router()` 内追加 `@router.<method>` 处理器，`main.py` 的 `include_router` 无需改动 |
| 新增环境变量 | 在 `config.py` 增加读取函数（命名对齐 Go 版 `EDITH_*`），由 `main.py` 读取后注入目标模块 |
| 更换数据库实现 | 替换 `database.open_database` 返回的连接（各模块只接收 `aiosqlite.Connection` 对象），注意单连接串行语义 |
| 调整错误响应契约 | 修改 `http_utils.register_error_handler`（需同步前端与 Go 版契约） |

---

**说明**：本 README 描述包根（组合根 + 共享基础设施）；各业务模块的详细文档见 `internal/<模块>/README.md`。
