# userconfig（用户配置）模块 README

## 第一部分：模块名称与简介

**模块名**：`userconfig`（Python 包路径：`edith_backend.internal.userconfig`）

**一句话定位**：管理每个用户的 Agent 设置（人格、默认模型、时区）、模型供应商密钥、外部渠道账号到 Clerk 用户的绑定，以及 MCP 服务配置与运行时工具连接；是其他业务模块（models / cronjob / agentrun / gateway）共用的用户级底层能力模块。

**对应 Go 版源码路径**：`backend-v2/internal/userconfig/`（对照文件：`bindings.go`、`mcp.go`、`mcp_headers.go`、`mcp_http.go`、`mcp_runtime.go`、`mcp_validation.go`、`module.go`、`providers.go`、`routes.go`、`schema.go`、`settings.go`、`settings_http.go`、`types.go`）。

**文件清单**（文件名 + 一句话职责）：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 包导出面：对外公开 `Module`、`new_module` 及各子能力类（`Settings`/`Providers`/`Bindings`/`MCP`/`HTTP`）与常用类型。 |
| `module.py` | 组装入口：`new_module(db, default_model_id)` 先建表、再创建各子能力并组装 HTTP（对应 Go 版 `module.go` 的 `Module` 与 `New`）。 |
| `schema.py` | 表结构：本模块拥有的 6 张表的 DDL（`users`、`user_agents`、`user_providers`、`user_mcp_servers`、`user_mcp_headers`、`user_channel_bindings`）。 |
| `store.py` | 持久化辅助：`ensure_user()` 确保用户记录存在，首次访问建立空白用户（对应 Go 版 `bindings.go` 中的 `ensureUser`）。 |
| `types.py` | 类型定义：内部类型用 dataclass、HTTP 契约用 Pydantic（camelCase 对齐 Go 版 json tag）。 |
| `settings.py` | 用户 Agent 设置（人格 / 默认模型 / 时区）的读写。 |
| `providers.py` | 用户模型供应商密钥的读写与「只暴露状态」的查询。 |
| `bindings.py` | 外部渠道账号到 Clerk 用户的映射（绑定 / 查询）。 |
| `mcp.py` | MCP 服务配置的读写与输入安全校验（URL/传输方式/Header 校验）。 |
| `mcp_http.py` | MCP 服务配置的 HTTP 管理路由（`build_mcp_router` 工厂函数）。 |
| `mcp_runtime.py` | MCP 运行时：连接用户启用的 MCP 服务、注册工具、执行工具调用。 |

## 第二部分：架构设计

### ① 分层

本模块自底向上可分为 5 层，均为同包内模块，由 `module.py` 显式装配：

| 层次 | 所在文件 | 说明 |
| --- | --- | --- |
| 组装层 | `module.py` | `Module` 聚合各子能力；`new_module()` 是唯一入口工厂。 |
| HTTP 边界层 | `http.py`、`mcp_http.py` | 定义 `APIRouter`，做参数校验与异常→`EDITHError` 映射，不含业务 SQL。 |
| 服务层（领域能力） | `settings.py`、`providers.py`、`bindings.py`、`mcp.py`、`mcp_runtime.py` | 业务规则与持久化读写；`mcp_runtime.py` 负责对外部 MCP 服务的连接。 |
| 数据访问层 | 各服务类内部 + `schema.py` + `store.py` | 各服务类持有 `aiosqlite.Connection` 直接执行 SQL；`schema.py` 建表；`store.py` 提供 `ensure_user` 公共辅助。 |
| 类型定义层 | `types.py`、`mcp.py`（内嵌 dataclass/Pydantic 模型） | 内部流转用 dataclass，HTTP 进出用 Pydantic（camelCase）。 |

注意：本模块**没有独立的数据访问类**，SQL 直接写在服务类方法中（如 `Settings.load`、`MCP.list` 内联 `SELECT`）。

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | 所有服务类 `__init__(self, db)`；`HTTP.__init__(settings, providers, default_model_id)`；`Module.__init__(...)` | 数据库连接与依赖对象由调用方（`new_module`、`main.py`）注入，模块内部不自行创建。 |
| 工厂函数 | `module.py: new_module()`；`mcp_http.py: build_mcp_router(mcp)` | 统一创建整模块 / 创建 MCP 路由。 |
| 仓储式封装 | `Settings`、`Providers`、`MCP`、`Bindings` | 每个类封装一类实体的增删改查，隐藏 SQL 与表结构细节。 |
| 适配器 | `mcp_runtime.py`（`_connect_server`、`MCPToolContext`） | 把官方 `mcp` SDK（`ClientSession`、`sse_client`、`streamable_http_client`）适配为内部统一工具上下文，并将 MCP 工具定义转换为 OpenAI function-calling 格式。 |
| 策略分派（简单分支） | `mcp_runtime._connect_server`：按 `server.transport`（`"sse"` / `"streamable"`）选择不同客户端 | 传输方式策略化，新增传输只需增加分支。 |
| DTO / 契约分离 | `types.py`、`mcp.py` | 内部 dataclass（`AgentSettings`、`MCPServer` 等）与对外 Pydantic 模型（`SettingsRequest`/`SettingsResponse`、`MCPServerRequest`/`MCPServerResponse`）分离，`alias_generator=to_camel` 对齐 Go json tag。 |
| 空对象 / 首次初始化 | `store.ensure_user` + `Settings.load` 返回空白 `AgentSettings` | 首次访问自动建立用户记录，调用方无需预创建。 |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `new_module(db, default_model_id) -> Module` | `module.py` | 模块入口：校验 db、`create_schema`、装配 `Settings/Providers/Bindings/MCP/HTTP`。 |
| `Module` | `module.py` | 能力集合：`settings`、`providers`、`bindings`、`mcp`、`http` 五个公开字段。 |
| `create_schema(db)` | `schema.py` | 执行 6 张表的 `CREATE TABLE IF NOT EXISTS`。 |
| `ensure_user(db, user_id)` | `store.py` | `INSERT OR IGNORE INTO users`，保证用户记录存在。 |
| `Settings.load / save` | `settings.py` | 读/写 `user_agents` 表（personality、default_model_id、timezone），`save` 用 UPSERT。 |
| `Settings.load_personality / load_default_model_id / load_timezone` | `settings.py` | 便捷读取，均基于 `load()`。 |
| `Settings.save_timezone(user_id, timezone)` | `settings.py` | 读改写时区（cronjob 定时任务用）。 |
| `Providers.list_statuses(user_id)` | `providers.py` | 返回 `list[ProviderStatus]`（`provider_id` + `has_api_key`），**只暴露状态、不暴露密钥**。 |
| `Providers.save(user_id, credentials)` | `providers.py` | UPSERT 供应商密钥；`api_key is None` 的项跳过（不覆盖）。 |
| `Providers.load_api_key(user_id, provider_id) -> str` | `providers.py` | 服务端读取明文密钥供 Agent 运行使用；未找到抛 `LookupError`。 |
| `Bindings.bind(channel, external_user_id, clerk_user_id)` | `bindings.py` | UPSERT 渠道绑定到 `user_channel_bindings`。 |
| `Bindings.to_clerk_user_id(channel, external_user_id) -> tuple[str\|None, bool]` | `bindings.py` | 渠道账号→Clerk 用户查询，未绑定时 `found=False`。 |
| `MCP.list / create / update / delete` | `mcp.py` | `user_mcp_servers` + `user_mcp_headers` 的增删改查；`update` 仅 upsert 提交的 header（值为 None 的不覆盖），`delete` 级联删除 headers。 |
| `_validate_input(input_, require_header_values)` | `mcp.py` | MCP 输入安全校验（名称、transport 白名单、URL 协议与本地地址、header 名/去重/必填值）。 |
| `HTTP` | `http.py` | 用户设置 + 供应商密钥的 HTTP 边界；`_write_settings` 组装响应（默认模型兜底 `default_model_id or self.default_model_id`）。 |
| `build_mcp_router(mcp) -> APIRouter` | `mcp_http.py` | MCP 管理路由；`_server_response` 将内部 `MCPServer` 转为对外 `MCPServerResponse`（header 只给 `has_value`）。 |
| `MCPRuntime.open_tools(user_id) -> MCPToolContext` | `mcp_runtime.py` | 列出启用服务并逐个连接注册工具；单服务失败跳过（降级）。 |
| `MCPToolContext` | `mcp_runtime.py` | 本次运行的工具集合：`has_tools` / `has(name)` / `tool_definitions()` / `execute(name, arguments)` / `close()`。 |
| `_toolset_name(server)` | `mcp_runtime.py` | 生成工具集前缀（服务名小写清洗 + 8 位短 id），工具重命名为 `{toolset}__{tool}` 避免冲突。 |

## 第三部分：与其他模块的联系

### ① 依赖关系

**内部模块依赖**（本模块是底层能力模块，只依赖基础设施，不依赖其他业务模块）：

| 被 import 的内部模块 | 用途 |
| --- | --- |
| `..http_utils`（`EDITHError`） | HTTP 边界统一抛出业务错误，由 `main.py` 注册的全局异常处理器转为 JSON。 |
| 同包子模块（`store`、`types`、`settings`、`providers`、`mcp` 等） | 各服务类之间及装配层互相引用。 |

**外部 SDK / 标准库依赖**：

| 依赖 | 用途 |
| --- | --- |
| `aiosqlite` | 全部持久化操作（异步 SQLite 连接，由 `main.py` 的 `open_database()` 创建后注入）。 |
| `fastapi`（`APIRouter`） | HTTP 路由定义。 |
| `pydantic`（`BaseModel`/`ConfigDict`/`Field`、`alias_generators.to_camel`） | HTTP 请求/响应契约与 camelCase 别名。 |
| `mcp`（`ClientSession`、`client.sse.sse_client`、`client.streamable_http.streamable_http_client`） | MCP 运行时连接用户 MCP 服务（仅 `mcp_runtime.py` 使用）。 |
| `httpx` | streamable HTTP 传输的自定义客户端（携带用户配置的 headers）。 |
| 标准库 | `re`（header 名校验、工具名前缀清洗）、`ipaddress`（本地地址拦截）、`urllib.parse.urlparse`（URL 校验）、`uuid.uuid4`（服务 ID）、`dataclasses`、`logging`、`json`。 |

### ② 数据流向（典型业务流转）

**流程 1：Web BFF 读取 / 保存用户设置与供应商密钥**
```
浏览器/BFF ──GET/PUT /internal/user-settings──▶ HTTP 边界（http.py）
   ├─ Settings.load(user_id)        ──▶ user_agents 表（personality/default_model_id/timezone）
   ├─ Providers.list_statuses(user_id) ──▶ user_providers 表（只取 has_api_key 布尔）
   └─ 响应 SettingsResponse：设置 + providers 状态（永不包含密钥明文）
```
`PUT` 时：`Settings.save`（UPSERT `user_agents`）+ `Providers.save`（UPSERT `user_providers`；`api_key` 空串视为"不覆盖"转为 `None`），随后返回最新状态。

**流程 2：一次 AgentRun 的配置加载（agentrun → userconfig）**
```
agentrun/configuration.py: Configuration.load(request)
   1. settings.load_default_model_id(user_id)   ── 请求未指定模型时取用户默认（再兜底平台 DEFAULT_MODEL_ID）
   2. providers.load_api_key(user_id, provider_id) ── 读取明文密钥；缺失抛 LookupError → 转 invalid_request
   3. settings.load_personality(user_id)         ── 拼入 system prompt
   4. mcp_runtime.open_tools(user_id)            ── 列出用户启用 MCP 服务并连接，
                                                    工具定义进入 ConfiguredRun.mcp_tools（MCPToolContext）
运行中：模型经 MCPToolContext.execute(prefixed_name, args) 调用远端工具
```

**流程 3：渠道消息绑定解析（gateway → userconfig）**
```
gateway/service.py: Gateway.run(msg)
   非可信渠道：bindings.to_clerk_user_id(channel, external_user_id)
       ├─ 未绑定 → Error("identity_not_bound")
       └─ 已绑定 → 得到 clerk_user_id，作为 AgentRun 的 user_id
```

**流程 4：cronjob 时区读写（cronjob → userconfig）**
```
cronjob/http.py: save_timezone(user_id, timezone)  ──▶ Settings.save_timezone（写 user_agents.timezone）
cronjob/schedule_rules.py: load_timezone(user_id)  ──▶ Settings.load_timezone（定时规则按用户时区计算）
```

**流程 5：可用模型列表（models → userconfig）**
```
models/http.py: GET /internal/available-models ──▶ providers.list_statuses(userId)
   ── 与模型目录做交集：只有用户已配密钥的供应商模型才返回
```

### ③ 接口调用

**本模块暴露的 HTTP 路由**（经 `main.py` `app.include_router(...)` 注册，`userId` 由 BFF 注入）：

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/internal/user-settings` | 读取用户设置与供应商状态（query 参数 `userId` 必填）。 |
| PUT | `/internal/user-settings` | 保存设置与供应商密钥（`SettingsRequest` 体，`userId`/`defaultModelId` 必填）。 |
| GET | `/internal/mcp-servers` | 列出用户 MCP 服务（query 参数 `userId` 必填）。 |
| POST | `/internal/mcp-servers` | 创建 MCP 服务（新 header 必须有值）。 |
| PUT | `/internal/mcp-servers/{server_id}` | 更新 MCP 服务（值为空的 header 不覆盖）。 |
| DELETE | `/internal/mcp-servers/{server_id}` | 删除 MCP 服务（级联删除 headers，返回 204）。 |

**内部方法及调用方**：

| 被调方法 | 调用方（模块/位置） | 用途 |
| --- | --- | --- |
| `Settings.load_default_model_id` / `load_personality` | `agentrun/configuration.py:60,79` | AgentRun 配置加载。 |
| `Settings.save_timezone` / `load_timezone` | `cronjob/http.py:57`、`cronjob/schedule_rules.py:33` | 定时任务时区维护。 |
| `Providers.load_api_key` | `agentrun/configuration.py:74` | AgentRun 读取密钥。 |
| `Providers.list_statuses` | `models/http.py:42` | `/internal/available-models` 过滤可用模型。 |
| `Bindings.to_clerk_user_id` | `gateway/service.py:37` | 渠道身份解析。 |
| `MCPRuntime.open_tools` | `agentrun/configuration.py:116` | 本次运行的 MCP 工具上下文。 |
| `MCP.*`（list/create/update/delete） | `mcp_http.build_mcp_router` | HTTP 管理接口的后端服务。 |
| `new_module(db, DEFAULT_MODEL_ID)` | `main.py:59` | 组合根装配；随后 `users.http.router`、`build_mcp_router(users.mcp)` 注册路由，`MCPRuntime(users.mcp)` 供 agentrun 使用。 |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **首次访问自动建户**：`ensure_user`（`store.py`）在一切读写前 `INSERT OR IGNORE` 用户记录，`Settings.load` 查不到记录时返回空白 `AgentSettings`，业务层无需区分新老用户。
2. **密钥安全边界**：
   - 对浏览器只暴露 `has_api_key` 布尔（`Providers.list_statuses`、`SettingsResponse.providers`、`MCPHeaderState.has_value`），响应模型里没有任何密钥字段；
   - 明文密钥仅经 `Providers.load_api_key` 在服务端供 Agent 运行使用（`agentrun`）；
   - `PUT /internal/user-settings` 中 `api_key` 为空字符串视为"不覆盖"（转为 `None`），`Providers.save` 对 `None` 跳过；MCP header 更新同理（`value is None` 不覆盖）。
3. **MCP 输入安全校验**（`mcp.py:_validate_input`，创建时 `require_header_values=True`、更新时 `False`）：
   - 名称必填；transport 仅允许 `streamable` / `sse`；
   - URL 必须为 `http`/`https` 且存在 hostname；拒绝 `localhost`、`*.localhost`、回环/私有/链路本地/未指定 IP 地址（防 SSRF 类本地地址）；
   - header 名必须匹配 `^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$`，大小写不敏感去重，新 header 必须带值。
4. **MCP 工具命名空间隔离**：`_toolset_name` 生成 `{清洗后的服务名}_{8位短id}` 前缀，工具重命名为 `{toolset}__{原名}`，避免多服务同名工具冲突；`execute` 按前缀名查回原名调用 `session.call_tool`。
5. **MCP 运行时降级策略**：`open_tools` 逐个连接启用服务，单个服务连接失败只 `logger.warning` 并跳过，不影响本次运行其他服务；关闭时逐个 `__aexit__` 并吞掉关闭异常。
6. **工具调用结果规范化**：`MCPToolContext.execute` 把 `CallToolResult` 文本内容拼接返回；`is_error=True` 或未知工具名时返回 `{"error": ...}` JSON 字符串。
7. **绑定语义**：`user_channel_bindings` 以 `(channel, external_user_id)` 为联合主键，重复绑定走 UPSERT 覆盖归属。
8. **级联清理**：删除 MCP 服务时同步删除其 headers（`mcp.delete`）；`mcp.create` 只插入有值的 header。

### ② 配置项

**无**。本模块所有文件均未读取环境变量或应用配置（未发现 `os.getenv` / `config` 引用）；模块所需的唯一外部参数 `default_model_id` 由组合根 `main.py:59` 注入（来源为 `models.DEFAULT_MODEL_ID = minimax.m3`，见 `internal/models/catalog.py`）。

> 说明：`EDITH_E2B_TEMPLATE` 等环境变量属于 sandbox 模块（`main.py:66`），与本模块无关。

### ③ 异常处理

| 异常类型 | 抛出位置 | 处理策略 |
| --- | --- | --- |
| `ValueError` | `bind`/`to_clerk_user_id`、`Settings`/`Providers`/`MCP.create` 的参数校验 | 由调用方捕获映射：`gateway/service.py:40` 捕获转 `Error("internal_error", ...)`；其余为防御性校验（正常情况下不会触发）。 |
| `InvalidMCPServer` | `mcp.py:_validate_input` | HTTP 层（`mcp_http.py`）捕获转 `EDITHError(400, "invalid_mcp_server", msg)`。 |
| `MCPServerNotFound` | `mcp.update` / `mcp.delete`（rowcount==0） | HTTP 层转 `EDITHError(404, "mcp_server_not_found", ...)`。 |
| `LookupError`（密钥缺失） | `providers.load_api_key` | `agentrun/configuration.py:75` 捕获转 `Error("invalid_request", ...)`。 |
| `EDITHError` | `http.py` / `mcp_http.py` | `http_utils.register_error_handler`（`main.py` 注册）全局转为 `{"type","message"}` JSON 响应。 |
| `RequestValidationError`（Pydantic 校验失败，含未知字段） | FastAPI 层 | 全局处理器统一转 `400 {"type":"invalid_request"}`，对齐 Go 版 `ReadJSON` 的 `DisallowUnknownFields`。 |
| MCP 连接/初始化/列工具异常（任意 `Exception`） | `mcp_runtime.open_tools` | 降级：`logger.warning` 记录后跳过该服务，继续连接其余服务；连接关闭异常被吞掉（`noqa: BLE001, S110`）。 |

### ④ 扩展点

1. **新增模型供应商**：本模块无需改动。供应商/模型目录在 `models` 模块（`catalog.py`）；用户在前端通过 `PUT /internal/user-settings` 提交 `providerId + apiKey` 即持久化到 `user_providers`，AgentRun 自动经 `Providers.load_api_key` 读取。
2. **新增 MCP 传输方式**：两处修改——`mcp.py:_validate_input` 的 transport 白名单（`("streamable", "sse")`）加入新值；`mcp_runtime.py:_connect_server` 的 `if server.transport == "sse"` 分支处新增对应客户端的建立逻辑。
3. **新增 MCP 能力（prompts / resources 等）**：可在 `mcp_runtime._connect_server` 连接后、或 `MCPToolContext` 上扩展（当前仅注册 tools，见 `mcp_runtime.py:101-115`）。
4. **新增 HTTP 路由**：在 `http.py:_build_router` 或 `mcp_http.build_mcp_router` 内新增端点（复用 `EDITHError` 与 Pydantic 契约），无需改动 `main.py`（已整体 include router）。
5. **新增存储字段 / 表**：改 `schema.py:_STATEMENTS`（DDL）+ 对应服务类的 SQL；注意当前建表是 `CREATE TABLE IF NOT EXISTS`，**未发现任何迁移/版本化机制**——对已存在的库新增列需自行处理（`ALTER TABLE` 或重建，当前代码未覆盖）。
6. **新增模块级能力**：在 `module.py:new_module` 中创建实例并挂到 `Module` 上，再在 `main.py` 装配处引用（如现有 `MCPRuntime(users.mcp)` 的接入方式）。
7. **工具集前缀策略**：多服务同名工具冲突时，可调整 `mcp_runtime._toolset_name` 的命名规则（当前为 `{服务名}_{8位id}`）。

---

**附：本模块拥有的数据库表**（`schema.py`）

| 表名 | 主键 | 说明 |
| --- | --- | --- |
| `users` | `user_id` | 用户记录（首次访问自动创建）。 |
| `user_agents` | `user_id` | 用户 Agent 设置（personality / default_model_id / timezone）。 |
| `user_providers` | `(user_id, provider_id)` | 用户供应商密钥（api_key 明文仅服务端可读）。 |
| `user_mcp_servers` | `server_id` | MCP 服务配置（name / url / transport / enabled / 时间戳）。 |
| `user_mcp_headers` | `(server_id, header_name)` | MCP 请求头（值密文与否由上层决定，当前为明文存储）。 |
| `user_channel_bindings` | `(channel, external_user_id)` | 外部渠道账号 → Clerk 用户映射。 |
