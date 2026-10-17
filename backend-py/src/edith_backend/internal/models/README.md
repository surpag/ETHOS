# models 模块（模型目录与供应商）

## 第一部分：模块名称与简介

**模块名**：`models`（模型目录，Model Catalog）

**一句话定位**：维护 EDITH 的内置模型与供应商静态定义，并叠加用户在设置页自建的供应商/模型（存 DB），向浏览器提供合并后的目录与"可用模型"查询，向 agentrun 提供按稳定 ID 查找模型定义的能力。

**对应 Go 版源码路径**：`backend-v2/internal/models/`（`http.go`、`routes.go`、`catalog.go`、`module.go`、`types.go`）

**文件清单**：

| 文件 | 一句话职责 |
| --- | --- |
| `http.py` | HTTP 边界：目录查询 2 个路由 + 自定义供应商/模型的增删改查与探测 7 个路由。业务校验失败统一 400。 |
| `types.py` | 类型定义：Pydantic v2 的 HTTP 契约（camelCase）与内部不可变 dataclass `ModelDefinition`；含 `custom.*` ID 前缀常量。 |
| `catalog.py` | 内置供应商/模型常量（只读权威源）及 `Catalog` 查询类（同步查内置、异步查合并结果）。 |
| `custom.py` | `CustomModels`：用户自定义供应商与模型的存储；API Key 一律转调 `userconfig.providers`。 |
| `endpoint.py` | 自定义端点校验（SSRF）、远端 `/models` 发现、`max_tokens=1` 连通性探测。 |
| `schema.py` | `user_custom_providers` / `user_custom_models` 建表与幂等补列。 |
| `module.py` | 模块组装入口：`new_module(providers, custom)` 显式装配 `Catalog` 与 `HTTP` 并返回 `Module`。 |
| `__init__.py` | 包导出面：对外公开 `Catalog`、`CustomModels`、`HTTP`、`Module`、`new_module`、`create_schema` 等。 |

> 注：目录下无独立 `routes.go` 对应文件，路由注册内联在 `http.py` 的 `_build_router()` 中。

## 第二部分：架构设计

### ① 分层

按代码实际组织，本模块自下而上分为四层：

| 层 | 载体 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | 两套类型：Pydantic `BaseModel`（HTTP 契约，`alias_generator=to_camel` 输出 camelCase）与 `@dataclass(frozen=True)` 的 `ModelDefinition`（内部定义，不参与序列化）。 |
| 数据/目录层 | `catalog.py` | 模块级工厂函数 `provider_definitions()` / `model_definitions()` 构造静态目录数据；`Catalog` 类封装查询（返回拷贝、防御性只读）。 |
| 服务/组装层 | `module.py` | `new_module(providers)` 工厂创建 `Catalog` + `HTTP` 并组装成 `Module`，校验依赖非空。 |
| HTTP 边界层 | `http.py` | `HTTP` 类持有 `Catalog` 与 `Providers`，构建 `APIRouter` 暴露两个 GET 路由。 |

本模块**不直接访问数据库**：供应商密钥状态查询委托给 `userconfig.providers.Providers`（数据访问层在 userconfig 模块内）。

### ② 设计模式

| 模式 | 说明 | 代码位置 |
| --- | --- | --- |
| 依赖注入（构造器注入 + 显式装配） | `HTTP.__init__(catalog, providers)`、`Module.__init__(catalog, http)` 均由 `new_module(providers)` 注入；最终由组合根 `main.py` 第 60 行 `new_models_module(users.providers)` 显式装配。 | `module.py`、`http.py`、`main.py:60` |
| 工厂函数 | `new_module()`（模块工厂）、`provider_definitions()` / `model_definitions()`（目录数据工厂）。 | `module.py:25`、`catalog.py:41/51` |
| 数据传输对象（DTO）/投影 | `ModelDefinition.to_info()` 把内部完整定义投影为浏览器可见的 `ModelInfo`（隐藏 `context_window` / `api_model` / `base_url` / `extra_fields`）。 | `types.py:84` |
| 不可变对象 | `@dataclass(frozen=True)` 保证 `ModelDefinition` 不可变；`Catalog` 各查询方法返回 `list(...)` 拷贝（对应 Go 版注释"返回复制后的目录"）。 | `types.py:65`、`catalog.py:144/148/152` |
| 模块门面 | `Module` 聚合 `catalog` 与 `http` 两个能力，供组合根统一取用（对应 Go 版 `Module` struct）。 | `module.py:17` |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `HTTP` | `http.py` | `HTTP.__init__(self, catalog: Catalog, providers: Providers)`；`_build_router()` 返回含两个 GET 路由的 `APIRouter`。 |
| `Catalog` | `catalog.py` | `__init__(self)` 载入内置目录；`providers() -> list[ProviderInfo]`、`models() -> list[ModelInfo]`、`definitions() -> list[ModelDefinition]`、`find(model_id: str) -> ModelDefinition \| None`、`registered() -> dict[str, ModelDefinition]`。 |
| `ModelDefinition` | `types.py` | 不可变内部定义：`id`、`provider_id`、`name`、`context_window`、`api_model`、`base_url`、`vision`、`reasoning_options`、`extra_fields`、`does_not_report_cached_prompt_tokens`；`to_info() -> ModelInfo` 投影。 |
| `provider_definitions()` | `catalog.py` | 返回 5 个供应商的 `ProviderInfo` 列表（DeepSeek / MiniMax / 阶跃星辰 / 阶跃星辰 (Step Plan) / 小米 MiMo）。 |
| `model_definitions()` | `catalog.py` | 返回 9 个 `ModelDefinition` 列表（详见第四部分）。 |
| `new_module(providers)` | `module.py` | 模块工厂：`providers is None` 时抛 `ValueError`；否则组装并返回 `Module`。 |
| `Module` | `module.py` | `catalog: Catalog`、`http: HTTP` 两个能力字段。 |
| `ProviderInfo` / `ReasoningOption` / `Capabilities` / `ModelInfo` / `CatalogResponse` / `AvailableResponse` | `types.py` | Pydantic v2 HTTP 契约模型，均配置 `alias_generator=to_camel, populate_by_name=True`。 |
| 稳定 ID 常量（`DEEPSEEK_PROVIDER_ID` 等、`DEFAULT_MODEL_ID`） | `catalog.py` | 供应商/模型稳定 ID 与默认模型（`DEFAULT_MODEL_ID = minimax.m3`）。 |

## 第三部分：与其他模块的联系

### ① 依赖关系

- **内部依赖**：
  - `..userconfig.providers.Providers` —— 供应商密钥状态查询（`list_statuses`），仅用于 HTTP 的"可用模型"过滤；运行时经构造器注入（`module.py`/`http.py` 中为 `TYPE_CHECKING` 导入 + 构造器注入，不产生运行时 import）。
  - `..http_utils.EDITHError` —— 业务错误，供 `list_available` 抛出后由全局异常处理器转 JSON。
- **外部 SDK**：`fastapi.APIRouter`（路由）、`pydantic`（`BaseModel` / `ConfigDict` / `alias_generators.to_camel`）、标准库 `dataclasses` / `typing`。**不直接使用** OpenAI SDK 或 aiosqlite。

### ② 数据流向

**典型流转 1（目录/可用模型查询，浏览器 → HTTP 边界）**：

```
浏览器 GET /internal/available-models?userId=xxx
  → HTTP.list_available
  → userconfig.Providers.list_statuses(userId)   // 读 user_providers 表（DB 访问在 userconfig 模块）
  → Catalog.definitions() 按 has_api_key 过滤
  → AvailableResponse {models: [ModelInfo]}      // camelCase JSON 返回浏览器
```

**典型流转 2（一次 Agent 运行，目录 → agentrun）**：

```
用户请求指定 model_id（或未指定）
  → agentrun.configuration.RunConfigurations.load()
  → Catalog.find(model_id)                        // 解析 ModelDefinition
  → 校验 definition.vision 是否支持图片输入
  → Providers.load_api_key(user_id, definition.provider_id)
  → agentrun.execution：AsyncOpenAI(api_key, base_url=definition.base_url)
      .chat.completions.create(model=definition.api_model, extra_body=definition.extra_fields)
```

即：**本模块只产出一份静态目录（定义 + 查询），运行期数据（用户密钥、消息）全部来自其他模块**。

### ③ 接口调用

**暴露的 HTTP 路由**（由组合根 `main.py:106` `app.include_router(model_catalog.http.router)` 挂载）：

| Method | Path | 入参 | 响应 | 调用方 |
| --- | --- | --- | --- | --- |
| GET | `/internal/models` | 无 | `CatalogResponse {providers, models}` | Web BFF / 前端（模型下拉、供应商配置页） |
| GET | `/internal/available-models` | query `userId`（必填） | `AvailableResponse {models}`（仅含已配置密钥供应商的模型） | Web BFF / 前端（可用模型筛选） |

**内部方法及被调用方**：

| 方法 | 被谁调用 |
| --- | --- |
| `Catalog.find()` | `agentrun.configuration.RunConfigurations.load()`（`configuration.py:64`） |
| `Catalog.definitions()` / `providers()` / `models()` | `http.py` 内部；`main.py:86` 把 `model_catalog.catalog` 注入 `agentrun.Dependencies.models` |
| `new_module()` | `main.py:60` |
| `DEFAULT_MODEL_ID` | `main.py:59`（传给 userconfig）、`agentrun/configuration.py:62`（默认模型兜底） |
| `Catalog.registered()` | **当前 Python 代码库内未被调用**（Go 版用于 ManagedRunner 注册；Python 版运行路径改用 `find()` + 直接构造 OpenAI 客户端）——待确认是否保留备用。 |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **静态内置目录**：5 个供应商、9 个模型定义，全部硬编码在 `catalog.py`：

   | 模型稳定 ID | 供应商 | 上下文窗口 | api_model | base_url |
   | --- | --- | --- | --- | --- |
   | `deepseek.v4.flash` | deepseek | 1M | `deepseek-v4-flash` | `https://api.deepseek.com` |
   | `deepseek.v4.pro` | deepseek | 1M | `deepseek-v4-pro` | `https://api.deepseek.com` |
   | `minimax.m3`（默认模型） | minimax | 1M | `MiniMax-M3` | `https://api.minimaxi.com/v1`（`extra_fields={"reasoning_split": True}`、`vision=True`） |
   | `stepfun.step-3.7-flash` | stepfun | 256K | `step-3.7-flash` | `https://api.stepfun.com/v1`（`vision=True`） |
   | `stepfun.step-3.5-flash` | stepfun | 256K | `step-3.5-flash` | `https://api.stepfun.com/v1`（`vision=True`） |
   | `stepfun_plan.step-3.7-flash` | stepfun_plan | 256K | `step-3.7-flash` | `https://api.stepfun.com/step_plan/v1`（`vision=True`） |
   | `stepfun_plan.step-3.5-flash` | stepfun_plan | 256K | `step-3.5-flash` | `https://api.stepfun.com/step_plan/v1` |
   | `xiaomi.mimo-v2.5-pro` | xiaomi | 1M | `mimo-v2.5-pro` | `https://api.xiaomimimo.com/v1` |
   | `xiaomi.mimo-v2.5` | xiaomi | 1M | `mimo-v2.5` | `https://api.xiaomimimo.com/v1`（`vision=True`） |

   > 注：`catalog.py:1` 的模块 docstring 写"8 个内置模型定义"，实际代码为 **9 个**，存在出入，待确认。
   > 注：所有定义均未填充 `reasoning_options`（默认空元组）与 `does_not_report_cached_prompt_tokens`（默认 `False`）；`types.py` 中 `ModelInfo.reasoning_options` 当前恒为空列表。`does_not_report_cached_prompt_tokens` 在 Python 代码库中暂无消费方。

2. **内部/对外分离**：`ModelDefinition`（含 `context_window`/`api_model`/`base_url`/`extra_fields` 等运行参数）不暴露给浏览器；`to_info()` 只投影 `id`、`provider_id`、`name`、`reasoning_options`、`capabilities.vision`。

3. **可用性过滤**：`/internal/available-models` 依据 `user_providers` 表中该用户 `api_key != ''` 的供应商，过滤目录模型；`userId` 缺失直接 400。

4. **依赖强校验**：`new_module(None)` 抛 `ValueError("models requires provider credentials")`，保证模块不脱离凭据能力被组装。

### ② 配置项

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `EDITH_ALLOW_LOCAL_MODEL_ENDPOINT` | `false` | 允许自定义端点使用 `http://` 与回环/私网地址（自建 Ollama、vLLM、内网网关）。**云元数据端点始终拒绝**，不受此开关影响。 |
| `EDITH_MODEL_PROBE_TIMEOUT_S` | `15` | 「拉取模型列表」与「测试连接」的请求超时（秒）；两条路径都 `max_retries=0`，不叠加 SDK 重试。 |

内置供应商的 base_url 仍是 `catalog.py` 里的硬编码常量。组合根 `main.py` 中读取的 `EDITH_E2B_TEMPLATE`（`main.py:66`）、`EDITH_COS_*` 等属于 sandbox/images 模块，与本模块无关。

### ③ 异常处理

| 场景 | 异常/处理 | 策略 |
| --- | --- | --- |
| `/internal/available-models` 缺 `userId` | 抛 `EDITHError(400, "invalid_request", "userId is required")` | 由 `http_utils.register_error_handler` 全局处理器转为 `{"type","message"}` JSON（400） |
| `new_module` 收到 `None` providers | `ValueError("models requires provider credentials")` | 组装期即失败（启动时暴露），不进入运行期 |
| 请求体校验失败（含未知字段） | `RequestValidationError` | 全局处理器统一转 400 `invalid_request`（对齐 Go 版 `DisallowUnknownFields`） |

本模块内部无 `try/except` 捕获（不自行记录日志），错误统一上抛交由 `main.py` 注册的全局异常处理器。差异点（待确认是否有意简化）：Go 版 `http.go` 对 `providers == nil`（503 `providers_unavailable`）与 `ListStatuses` 失败（500 `provider_load_failed`）有显式错误分支，Python 版未实现这两条分支——`providers` 在 Python 版组装期已被 `new_module` 强校验非空，但 `list_statuses` 的 DB 异常在 `http.py` 中无显式捕获。

### ④ 扩展点

- **新增模型（推荐，无需改代码）**：用户在设置页「添加模型」填写真实 model 名即可，落在 `user_custom_models`；ID 前缀 `custom.model:` 与内置 ID 空间隔离，合并后自动进入聊天页可选列表。
- **内置模型（需要代码级参数时才走这条路）**：在 `catalog.py` 的 `model_definitions()` 中追加 `ModelDefinition`（`extra_fields`、`does_not_report_cached_prompt_tokens` 这类只有代码能表达的知识必须留在这里）；默认模型调整改 `DEFAULT_MODEL_ID`。
- **新增供应商**：在 `provider_definitions()` 中加入 `ProviderInfo`；密钥存储复用 userconfig 的 `user_providers` 表（`providers.save`/`list_statuses` 无需改动），`/internal/available-models` 即可自动纳入。
- **新增/调整 HTTP 路由**：在 `http.py` 的 `_build_router()` 内注册新路由；新的响应结构在 `types.py` 定义 Pydantic 模型（camelCase 别名）。
- **扩展对外字段**：在 `ModelDefinition` 增加内部字段 → 在 `to_info()` 投影 → 在对应 Pydantic 模型加字段（注意 `populate_by_name` + `alias_generator` 行为）。
- **对接运行期注册**：`Catalog.registered()` 已实现（返回 `{model_id: ModelDefinition}` 映射），当前无调用方；若未来需要注册式 Runner（对齐 Go 版 `ManagedRunner`），可直接消费该方法。

## 摘要

- 本 README 覆盖 models 模块四大部分：模块简介与文件清单（5 个 Python 文件）、架构分层（类型/目录/组装/HTTP 四层）与设计模式（DI、工厂、DTO 投影、不可变对象、模块门面）、与 userconfig/agentrun/main 的依赖与数据流、以及扩展说明。
- 关键发现 1：`catalog.py` docstring 称"8 个内置模型"，实际定义 9 个（deepseek×2、minimax×1、stepfun×2、stepfun_plan×2、xiaomi×2）。
- 关键发现 2：`Catalog.registered()` 在 Python 代码库中无调用方（Go 版用于 ManagedRunner 注册），agentrun 实际走 `find()` + 直接构造 AsyncOpenAI 客户端。
- 关键发现 3：`reasoning_options` 所有定义均未填充（对外恒为空列表）；`does_not_report_cached_prompt_tokens` 字段定义但无消费方。
- 关键发现 4：本模块不读任何环境变量、不直接访问 DB（密钥状态经 userconfig.Providers 委托）；异常处理依赖 http_utils 全局 handler，且未实现 Go 版的 `providers_unavailable`/`provider_load_failed` 两条错误分支。
