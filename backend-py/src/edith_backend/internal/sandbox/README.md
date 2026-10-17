# sandbox 模块 —— E2B 沙箱生命周期、文件与工具

## 第一部分：模块名称与简介

| 项目 | 内容 |
| --- | --- |
| 模块名 | `sandbox`（Python 包路径 `edith_backend.internal.sandbox`） |
| 一句话定位 | 按「用户 + 会话」隔离的 E2B 工作区管理：懒创建/恢复 Sandbox、用户上传文件的浏览/预览/上传/下载，以及暴露给 Agent 的文件与进程工具集。 |
| 对应 Go 版源码路径 | `backend-v2/internal/sandbox/`（`module.go`、`service.go`、`agent_input.go`、`http.go`、`routes.go`、`files.go`、`processes.go`、`tools.go`、`workspace.go`、`schema.go`、`types.go`，另有测试 `http_test.go`） |

### 文件清单

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块公开导出：`HTTP`、`AgentInput`、`SandboxToolSet`、`Service`、`WorkspaceLayout`、`create_schema` 及路径工具；对应 Go 版 `module.go` 的组装职责实际由 `main.py` 承担 |
| `service.py` | `Service`：维护 `user_sandboxes` 映射表，首次调用才创建远端 E2B Sandbox，之后按映射恢复连接 |
| `workspace.py` | `WorkspaceLayout` 固定目录约定（root/uploads/work/artifacts）+ `workspace_path`/`relative_path` 路径安全转换 |
| `agent_input.py` | `AgentInput.validate_uploads`：校验 Agent 本次运行可引用的用户上传文件（必须位于 `uploads/` 内且存在） |
| `tools.py` | `SandboxToolSet`：10 个 Agent 工具（6 个文件操作 + 4 个进程操作），OpenAI function calling 格式定义、`execute` 统一分发 |
| `http.py` | `HTTP`：`/internal/sandbox/files*` 四组路由（浏览/预览/上传/下载） |
| `schema.py` | `create_schema`：创建 `user_sandboxes` 表 |

Go 版文件与 Python 文件的对应关系：`service.py ↔ service.go`、`workspace.py ↔ workspace.go + files.go`（路径部分）、`agent_input.py ↔ agent_input.go`、`tools.py ↔ tools.go + files.go + processes.go`、`http.py ↔ http.go + routes.go`、`schema.py ↔ schema.go`、`types.go` 中的 `FileTreeResponse`/`FileContentResponse` 在 Python 版未单独定义类型，直接以内联 `dict` 返回（`http.py`/`tools.py`）。

## 第二部分：架构设计

### ① 分层

按代码实际分层，从外到内：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| HTTP 边界层 | `http.py` | `APIRouter` 定义 4 条路由；负责参数校验、把业务异常映射为 `EDITHError`，不直接触碰 E2B SDK |
| 服务层 | `service.py` | `Service` 封装沙箱生命周期（查映射 → 连接/创建 → 写映射），供 HTTP、工具集、Agent 输入校验共同复用 |
| 工具/输入层 | `tools.py`、`agent_input.py` | 面向 Agent 的工具集与上传文件校验，消费 `Service` 提供的沙箱句柄 |
| 数据访问层 | `service.py`（内联 SQL）、`schema.py` | 通过 `aiosqlite.Connection` 直查/直写 `user_sandboxes` 表；未设独立仓储类，SQL 内联在 `Service` 中 |
| 类型/常量层 | `workspace.py`、`schema.py` | `WorkspaceLayout` 目录约定、各阈值常量（读取上限、上传上限、命令超时、输出截断）、表结构 DDL |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `service.py:24-32`、`http.py:19-22`、`tools.py:43-44`、`agent_input.py:16-18`、`main.py:66-68` | `Service(db, template, volumes)` 显式注入 DB 连接、模板名、volume 服务；`HTTP`/`SandboxToolSet`/`AgentInput` 只依赖 `Service`，由组合根 `main.py` 显式装配 |
| 门面（Facade） | `service.py:43-85` | `Service` 把「映射查询 + E2B 连接/创建 + 映射写入」的完整生命周期收敛为 `workspace()`/`existing_workspace()` 两个入口，屏蔽 E2B SDK 细节 |
| 工厂函数 | `tools.py:21-33`（`_tool`）、`tools.py:36-37`（`_guide`） | 按 OpenAI function calling 格式批量生成工具定义（`type/function/name/description/parameters`），并拼接 `WORKSPACE.tool_guide()` 目录指南 |
| 策略式分发（Dispatcher） | `tools.py:77-86` | `execute` 用 `getattr(self, f"_op_{name}")` 按工具名路由到具体处理方法；未知工具返回错误 JSON 而非抛异常 |
| 单例常量对象 | `workspace.py:32`（`WORKSPACE = WorkspaceLayout()`） | 模块级共享的冻结 `dataclass`（frozen=True），统一目录约定 |
| 适配器 | `workspace.py:35-56` | `workspace_path`/`relative_path` 对外部路径做规范化与安全校验（拒绝绝对路径、`..`、空路径按 `allow_root` 放行），是对 E2B 文件路径 API 的薄封装 |
| 未发现状态机 | — | Sandbox 生命周期（超时暂停、自动恢复）由 E2B SDK `SandboxLifecycle(on_timeout="pause", auto_resume=True)` 托管（`service.py:75`），本模块未自行实现状态机 |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Service` | `service.py` | 会话↔沙箱生命周期管理。`__init__(db: aiosqlite.Connection, template: str, volumes: VolumeService)`；`async workspace(user_id, session_id) -> AsyncSandbox`（懒创建，首次才建远端资源）；`async existing_workspace(...) -> AsyncSandbox`（只连接不创建）；私有 `_load_sandbox_id(...)` 查映射 |
| `SandboxNotFound` | `service.py:19` | 会话无沙箱映射时抛出的自定义异常 |
| `AgentInput` | `agent_input.py` | `async validate_uploads(user_id, session_id, paths: list[str]) -> list[str]`：只接受当前用户当前会话 `uploads/` 内已存在的普通文件（去重、拒绝目录/越权路径） |
| `SandboxToolSet` | `tools.py` | `tool_definitions() -> list[dict]` 返回 10 个工具定义；`async execute(name, arguments, user_id, session_id) -> str` 统一执行入口（结果 JSON 序列化） |
| `_tool` / `_guide` | `tools.py` | 工具定义工厂 / 在工具描述后拼接 `WORKSPACE.tool_guide()` 目录指南 |
| `_op_sandbox_*`（10 个） | `tools.py` | 具体操作处理器：`list_files`/`read_file`/`write_file`/`make_directory`/`move_path`/`delete_path`/`run_command`/`start_process`/`list_processes`/`kill_process` |
| `_shorten` | `tools.py:178` | 命令输出截断到 `MAX_COMMAND_OUTPUT`（32KB），返回 `(内容, 是否截断)` |
| `HTTP` | `http.py` | `__init__(service)`；`_build_router() -> APIRouter` 构建 4 条路由；私有 `_existing(user_id, session_id, path, allow_root)` 做参数与映射的统一前置校验 |
| `WorkspaceLayout` / `WORKSPACE` | `workspace.py` | 目录约定：`root="/home/user"`、`uploads`、`work`、`artifacts`；`work_path()` 返回 `/home/user/work`；`tool_guide()` 生成工具路径说明文案 |
| `workspace_path` / `relative_path` | `workspace.py` | 工作区相对路径 → 沙箱绝对路径（含安全校验）/ 反向转换 |
| `create_schema` | `schema.py` | `async create_schema(db)`：`CREATE TABLE IF NOT EXISTS user_sandboxes` |

## 第三部分：与其他模块的联系

### ① 依赖关系

**本模块 import 的内部模块：**

| 被依赖方 | import 位置 | 用途 |
| --- | --- | --- |
| `volume.service.Service` | `service.py:13` | `mount_for_user(user_id)` 获取/创建用户 Skills Volume，返回挂载信息用于沙箱创建时 `volume_mounts` |
| `volume.types.CUSTOM_SKILLS_PATH` | `service.py:14` | 用户 Volume 在沙箱内的固定挂载目标路径 `/home/user/skills/custom` |
| `http_utils.EDITHError` | `http.py:11` | 统一业务错误（status + type + message 三元组），由全局错误处理器转为 `{"type","message"}` JSON |

**外部 SDK：** `e2b`（`AsyncSandbox`、`FileType`、`SandboxLifecycle`）、`aiosqlite`（DB 连接与 SQL）、`fastapi`（`APIRouter`、`UploadFile`、`StreamingResponse`）、`posixpath`/`dataclasses`（标准库）。

**被谁引用：** `main.py`（`SandboxService`、`SandboxToolSet`、`SandboxAgentInput`、`SandboxHTTP`、`create_sandbox_schema`）；`agentrun` 的 `Dependencies.files`（`service.py:38`、`configuration.py:46`）；聚合工具 `ToolRegistry`（`internal/tools.py`）。

### ② 数据流向

**典型流转 A：Agent 运行引用上传文件（`agentrun → agent_input → tools`）**

1. `agentrun` 收到带 `upload_paths` 的运行请求（`agentrun/configuration.py:83-89`），调用 `files.validate_uploads(user_id, session_id, paths)`（即 `SandboxAgentInput`）。
2. `AgentInput` 经 `Service.existing_workspace` 连接已有沙箱，逐个校验路径必须位于 `uploads/`、无 `..`、规范化后一致，且 `get_info` 确认非目录；校验失败抛 `ValueError`，被 `agentrun` 捕获转为 `Error("invalid_request", ...)`。
3. 校验通过的文件列表被注入系统提示词（`configuration.py:134-149`：要求 Agent 通过 Sandbox 文件工具从 `uploads/` 读取）。
4. Agent 在推理中调用 `sandbox_read_file`/`sandbox_run_command` 等工具 → `ToolRegistry.execute`（`internal/tools.py:24-28`）→ `SandboxToolSet.execute` → `Service.workspace`（连接或首次创建沙箱）→ E2B `files`/`commands` API → JSON 结果字符串返回 `agentrun`（`execution.py:178-179`）。

**典型流转 B：HTTP 上传 → 预览/下载**

1. `POST /internal/sandbox/files/upload`：校验参数与文件名（取 basename），读取文件（≤50MB）→ `Service.workspace`（首次调用会创建远端 Sandbox 并写入 `user_sandboxes` 映射）→ `sandbox.files.write` 写入 `/home/user/uploads/{name}` → 返回 `{path, name, size}`。
2. `GET /internal/sandbox/files` / `files/content`：`Service.existing_workspace` 只连接不创建，读目录（depth=1）或读文件前 32KB 预览。
3. `GET /internal/sandbox/files/download`：仅允许 `artifacts/` 下文件，以 `StreamingResponse` 附件形式返回。

### ③ 接口调用

**本模块暴露的 HTTP 路由**（均由 `main.py:109` `app.include_router(SandboxHTTP(sandboxes).router)` 注册）：

| Method | Path | 查询参数/请求体 | 说明 |
| --- | --- | --- | --- |
| GET | `/internal/sandbox/files` | `userId`、`sessionId`、`path`（可选） | 列出目录，depth=1，`allow_root=True` |
| GET | `/internal/sandbox/files/content` | `userId`、`sessionId`、`path` | 预览文本文件前 32KB，`allow_root=False`，含 NUL 字节拒绝 |
| POST | `/internal/sandbox/files/upload` | 表单 `userId`、`sessionId`、`file` | 上传文件到 `uploads/{basename}`，非空且 ≤50MB |
| GET | `/internal/sandbox/files/download` | `userId`、`sessionId`、`path` | 下载 `artifacts/` 下文件为附件流 |

**内部方法及其调用方：**

| 方法 | 调用方 |
| --- | --- |
| `Service.workspace(user_id, session_id)` | `tools.py:78`（工具执行）、`http.py:80`（上传）—— 首次调用创建远端沙箱 |
| `Service.existing_workspace(user_id, session_id)` | `http.py:32`（浏览/预览/下载）、`agent_input.py:26`（上传文件校验）—— 只连接不创建 |
| `SandboxToolSet.tool_definitions()` / `execute(...)` | `ToolRegistry`（`main.py:81` 聚合进 `ToolRegistry([SystemToolSet(), sandbox_tools, cron_jobs.tools])`），最终由 `agentrun/execution.py:178-179` 经 `tool_executor` 调用 |
| `AgentInput.validate_uploads(...)` | `agentrun/configuration.py:83-89`（作为 `Dependencies.files` 注入） |
| `create_schema(db)` | `main.py:56`（lifespan 启动时建表） |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **懒创建 + 恢复**：`workspace()` 先查 `user_sandboxes` 映射——命中则 `AsyncSandbox.connect` 并刷新 `updated_at`；未命中才 `AsyncSandbox.create`（`service.py:53-85`）。创建参数：`template`（来自 `EDITH_E2B_TEMPLATE`）、`metadata={"edith_user_id", "edith_session_id"}`、`lifecycle=SandboxLifecycle(on_timeout="pause", auto_resume=True)`、`volume_mounts={mount.name: CUSTOM_SKILLS_PATH}`（volume 不可用时为 `None`）。`existing_workspace()` 只读不写，供只读 HTTP 使用。
2. **映射表**：`user_sandboxes`（`schema.py`），字段 `user_id`/`session_id`（复合主键）、`sandbox_id`（UNIQUE）、`created_at`/`updated_at`。
3. **路径安全**：`workspace_path` 拒绝绝对路径、`..`、空路径（`allow_root=True` 时放行空/`.` 到根目录）；`relative_path` 做反向转换。`AgentInput` 额外要求 `uploads/` 前缀、`normpath` 后路径不变且不含 `..`。下载接口额外限定 `artifacts/` 前缀（403 拒绝其他目录）。
4. **工具集**：OpenAI function calling 格式定义 10 个工具（`tools.py:46-75`）；`execute` 用 `getattr` 反射分发，方法命名约定为 `_op_` + 工具名；工具内部异常一律转 `{"error": ...}` JSON 结果返回，不中断 agentrun。
5. **命令执行**：`sandbox_run_command` 在 `work/` 目录（`cwd=WORKSPACE.work_path()`）前台执行，默认超时 60s、输出截断 32KB；`sandbox_start_process` 后台启动；`sandbox_kill_process` 按 PID 结束；`sandbox_list_processes` 列出进程（cwd 若在根目录内转相对路径）。
6. **文件读写**：读文件默认截断 32KB（`DEFAULT_READ_LIMIT`）并返回 `truncated` 标记；HTTP 预览检测 NUL 字节以拒绝二进制文件。

### ② 配置项

| 配置 | 读取位置 | 说明 |
| --- | --- | --- |
| `EDITH_E2B_TEMPLATE` | `main.py:66` `os.getenv("EDITH_E2B_TEMPLATE", "default")` | E2B Sandbox 模板名，注入 `Service.template`。注意：`config.sandbox_template()`（`config.py:51-56`）定义了「必填且非空」校验，但 `main.py` 当前未调用它，而是以默认值 `"default"` 兜底（待确认是否为有意为之） |
| E2B API Key | 本模块代码中未显式读取 | 由 e2b SDK 内部从环境变量读取（具体变量名由 SDK 决定，代码未体现，待确认） |
| 模块内常量（非环境变量） | `http.py`/`tools.py`/`service.py` | `DEFAULT_READ_LIMIT = 32 * 1024`、`MAX_UPLOAD_SIZE = 50 << 20`（50MB）、`DEFAULT_COMMAND_TIMEOUT_MS = 60_000`、`MAX_COMMAND_OUTPUT = 32 * 1024`、`CONNECT_TIMEOUT_MS = 60 * 10`、`WORKSPACE` 目录约定 |

> ⚠️ 值得注意：`CONNECT_TIMEOUT_MS = 60 * 10`（即 600）传给 `AsyncSandbox.create/connect` 的 `timeout` 参数；若 e2b Python SDK 按毫秒解释该参数，则实际只有 0.6 秒，与 Go 版 `connectTimeout = 10 * time.Minute`（10 分钟）疑似量纲不一致（待确认，可能是 `60 * 10 * 1000` 之误）。

### ③ 异常处理

| 异常/场景 | 处理策略 | 代码位置 |
| --- | --- | --- |
| `userId`/`sessionId` 缺失 | 抛 `EDITHError(400, "invalid_request", ...)` | `http.py:25-26, 69-70` |
| 路径非法（绝对路径、`..`、空且不允许根） | `workspace_path` 抛 `ValueError` → 捕获后映射 `EDITHError(400, "invalid_path", ...)` | `http.py:28-30` |
| 沙箱映射不存在 | `SandboxNotFound` → 映射 `EDITHError(404, "sandbox_not_found", ...)` | `http.py:33-34` |
| 预览文件含 NUL 字节（二进制） | `EDITHError(422, "file_not_previewable", ...)` | `http.py:59-60` |
| 上传无文件/空文件/超 50MB/非法文件名 | `EDITHError(400, "invalid_upload", ...)` | `http.py:71-78` |
| 上传时沙箱不可用/E2B 异常 | 捕获 `Exception` → `EDITHError(502, "sandbox_files_unavailable", ...)` | `http.py:83-84` |
| 下载非 `artifacts/` 文件 | `EDITHError(403, "download_not_allowed", ...)` | `http.py:90-91` |
| 工具执行中任何异常 | `SandboxToolSet.execute` 捕获 `Exception`，转为 `{"error": str(exc)}` JSON 结果返回（降级，不抛给 agentrun） | `tools.py:82-86` |
| `AgentInput.validate_uploads` 校验失败 | 抛 `ValueError`，由 `agentrun/configuration.py:83-89` 捕获转为 `Error("invalid_request", ...)` | `agent_input.py:36, 41` |
| Volume 挂载失败（如 E2B 未启用 Volumes） | `volume.Service.mount_for_user` 内部降级返回 `None`，沙箱仍可创建，只是用户自定义 Skills 不可用 | `service.py:70-78`（降级逻辑在 volume 模块） |
| 请求体校验失败 | FastAPI `RequestValidationError` → 全局 handler 统一 400 `invalid_request`（`http_utils.py`） | 全局，非本模块 |

### ④ 扩展点

1. **新增 Agent 工具**：在 `tools.py` 中（1）用 `_tool(...)` 工厂添加一条工具定义到 `tool_definitions()`；（2）新增 `_op_sandbox_xxx` 方法（注意方法名必须是 `_op_` + 工具名，`execute` 靠 `getattr` 自动分发）。无需改动 `main.py` 与 `agentrun`，`ToolRegistry` 会自动收集。
2. **更换/新增 Provider**：当前 `Service` 硬编码 E2B SDK（`AsyncSandbox.create/connect` 与 `volume_mounts`）；替换需修改 `service.py` 的创建/连接逻辑，或将其抽象为接口后注入。
3. **新增 HTTP 路由**：在 `http.py` 的 `_build_router()` 内追加 `@router.<method>("/internal/sandbox/...")` 处理器，`main.py` 的 `include_router` 无需改动。
4. **新增存储/表结构**：在 `schema.py` 增加 DDL 并接入 `main.py` lifespan 建表；当前 SQL 内联在 `Service` 中，如需多表可先引入独立仓储层。
5. **调整目录约定**：修改 `workspace.py` 的 `WorkspaceLayout`（`root`/`uploads`/`work`/`artifacts`），并同步 `tool_guide()` 文案、`http.py` 下载白名单（`artifacts/` 前缀）等依赖约定处。
6. **安全边界调整**：`workspace_path`/`AgentInput` 的路径白名单策略（`uploads/` 前缀、禁止 `..`、下载限定 `artifacts/`）是集中定义的扩展/收紧点。
