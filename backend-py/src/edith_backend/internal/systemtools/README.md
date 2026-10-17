# systemtools 模块（系统内置工具集）

## 第一部分：模块名称与简介

- **模块名**：`systemtools`（系统内置工具集）
- **一句话定位**：提供不依赖用户身份与数据库的系统级 Agent 工具（当前仅 `get_current_time`），以"工具集"（tool set）形态注册进全局 `ToolRegistry`，供 Agent 运行时统一调度。
- **对应 Go 版源码路径**：`EDITH/backend-v2/internal/systemtools/`（`module.go`、`current_time.go`）

**文件清单**

| 文件 | 职责 |
| --- | --- |
| `tools.py` | 定义 `SystemToolSet`：内置工具 schema（`get_current_time`）与执行逻辑 |
| `__init__.py` | 模块出口，导出 `SystemToolSet`（`__all__ = ["SystemToolSet"]`） |

## 第二部分：架构设计

### ① 分层

本模块体积很小，模块内仅一层（工具定义 + 执行逻辑），不涉及 HTTP 边界与数据访问层：

| 层次 | 说明 | 代码位置 |
| --- | --- | --- |
| 工具定义层 | 以 OpenAI function-call 风格 dict 描述工具 schema | `tools.py` 的 `_tool()`、`SystemToolSet.tool_definitions()` |
| 工具执行层 | 按工具名分发并执行，返回 JSON 字符串结果 | `tools.py` 的 `SystemToolSet.execute()` |

在系统整体分层中，systemtools 处于能力提供方的最底层：由组合根 `main.py` 实例化后注入 `ToolRegistry`（`internal/tools.py`），再以 `tool_definitions` / `tool_executor` 形式注入 `agentrun` 执行引擎（`internal/agentrun/`）。

### ② 设计模式

| 模式 | 说明 | 代码位置 |
| --- | --- | --- |
| 工厂函数 | `_tool()` 按 name/description/properties/required 批量构造工具定义 dict，避免手写嵌套结构 | `tools.py` 第 13-25 行 |
| 依赖注入（构造注入） | 组合根显式 `SystemToolSet()` 实例化并注入 `ToolRegistry([...])`；本模块自身无任何可注入依赖 | `main.py` 第 81 行 |
| 组合 / 聚合 | `ToolRegistry` 聚合多个工具集（`SystemToolSet`、`SandboxToolSet`、`cron_jobs.tools`），统一对外提供定义与执行入口 | `internal/tools.py` 第 14-28 行 |
| 适配器（统一工具集契约） | `SystemToolSet` 实现 `tool_definitions()` + `execute(...)` 这一约定接口，把本模块工具适配成 `ToolRegistry` 可索引、`agentrun` 可调用的形态 | `tools.py` 第 29、39 行 |
| 无状态共享对象 | `SystemToolSet` 无实例状态，可安全共享给所有 Agent Run（对应 Go 版 `module.go` 中"可安全共享给所有 Agent Run"的设计） | `tools.py` 第 28 行 |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `_tool(name, description, properties, required) -> dict` | `tools.py` | 工厂函数：构造 OpenAI function-call 风格工具定义（`{"type": "function", "function": {...}}`） |
| `SystemToolSet` | `tools.py` | 系统工具集；无状态，实现工具集统一契约 |
| `SystemToolSet.tool_definitions() -> list[dict]` | `tools.py` | 返回内置工具定义列表（当前仅 `get_current_time`，参数为可选 `timezone`） |
| `SystemToolSet.execute(name, arguments, user_id, session_id) -> str` | `tools.py` | 执行具名工具：仅支持 `get_current_time`；未知工具/未知时区返回 `{"error": ...}` JSON；正常返回 `{"time": "...", "timezone": "..."}` JSON；`user_id`/`session_id` 参数保留但未使用 |
| `SystemToolSet`（导出） | `__init__.py` | 模块公开出口 |

## 第三部分：与其他模块的联系

### ① 依赖关系

- **内部模块**：无（本模块不 import 任何 `edith_backend` 内部模块）。
- **外部依赖**：仅 Python 标准库 —— `json`（结果序列化）、`datetime.datetime`（取当前时间）、`zoneinfo.ZoneInfo`（IANA 时区解析，依赖系统 tzdata）。
- **被谁依赖**：
  - `main.py`：`from .internal.systemtools import SystemToolSet`（第 37 行），实例化并注册进 `ToolRegistry`（第 81 行）；
  - `internal/tools.py`：`ToolRegistry` 在构造时调用 `tool_definitions()` 建立"工具名 → 工具集"索引（第 17-19 行），并在 `execute()` 中按名分发（第 24-28 行）；
  - `internal/agentrun/`（间接）：`Dependencies` 携带 `tool_definitions` / `tool_executor`（`service.py` 第 40-41、165-166 行），`execution.py` 第 178-179 行实际调用。

### ② 数据流向

典型业务流转（一次"问现在几点"的 Agent Run）：

```text
用户提问 ──► gateway（internal/gateway）──► agentrun Service（internal/agentrun/service.py）
  ──► execution.py：tools = self.tool_definitions（含 get_current_time schema）
  ──► 模型流式返回 tool_calls
  ──► tool_executor.execute("get_current_time", {"timezone": ...}, user_id, session_id)
  ──► ToolRegistry.execute 按名查 _by_name ──► SystemToolSet.execute
  ──► 返回 JSON 字符串（{"time": "...", "timezone": "..."} 或 {"error": ...}）
  ──► 作为 role="tool" 消息追加进 messages，进入下一轮模型调用（execution.py 第 183 行）
  ──► 最终经 StreamDecoder 推送给前端
```

要点：`user_id` / `session_id` 由 `agentrun` 透传进 `SystemToolSet.execute`，但本模块不使用它们——这正是"不依赖用户数据的系统工具"的定位。

### ③ 接口调用

- **HTTP 路由**：无。本模块不注册任何 HTTP 路由（`main.py` 中无对应 `include_router`）。
- **内部方法（被谁调用）**：
  - `tool_definitions()`：被 `main.py`（经 `ToolRegistry` 聚合后传给 `agentrun`）消费；
  - `execute(name, arguments, user_id, session_id)`：被 `ToolRegistry.execute` 调用（`internal/tools.py` 第 28 行），`ToolRegistry` 又被 `agentrun/execution.py` 第 178-179 行调用。
- **DB 表**：无（不涉及建表，`main.py` 未为本模块调用任何 `create_schema`）。

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **工具定义**：`tool_definitions()` 返回 OpenAI function-call 格式 schema；`get_current_time` 仅一个可选参数 `timezone`（如 `Asia/Shanghai`），`required` 为空列表。
2. **默认时区**：未传（或为空）`timezone` 时使用代码内常量 `"Asia/Shanghai"`（`tools.py` 第 42 行）。
3. **时间格式**：`%Y-%m-%d %H:%M:%S`。
4. **错误语义**：未知工具名、非法时区均返回 `{"error": ...}` JSON 字符串而非抛异常，与 Go 版 handler 返回 error 的职责对齐（`current_time.go` 中 `time.LoadLocation` 失败同样以错误返回）。
5. **与 Go 版对齐**：Python `SystemToolSet` ≈ Go `Module`（`module.go`）；工具定义与执行 ≈ `currentTimeTool()`（`current_time.go`）；Go 版 `currentTimeTool ──► systemtools.Tools ──► tools.Registry` 的装配关系与 Python 版 `SystemToolSet ──► ToolRegistry` 一致。

### ② 配置项

**无**。本模块不读取任何环境变量或配置（`EDITH_E2B_TEMPLATE` 等属于 `sandbox` 模块的配置，与本模块无关）；唯一"默认值"是代码内常量 `"Asia/Shanghai"`。

### ③ 异常处理

| 场景 | 处理方式 | 代码位置 |
| --- | --- | --- |
| 未知工具名（`name != "get_current_time"`） | 返回 `{"error": "unknown system tool: ..."}` JSON，不抛异常 | `tools.py` 第 40-41 行 |
| `ZoneInfo(timezone)` 构造失败（非法/未知时区） | `except Exception` 捕获，返回 `{"error": "unknown timezone ...: <exc>"}` JSON | `tools.py` 第 43-46 行 |
| 上游调用方兜底 | `agentrun/execution.py` 把返回的 JSON 字符串作为 tool 结果回传模型（降级为模型可见的错误信息），不中断 Run；无工具执行器时返回 `{"error": "no tool executor"}` | `execution.py` 第 171-183 行 |

未发现本模块使用日志（无 `logging` 调用）；无 HTTP 层，故不涉及 HTTP 错误映射。

### ④ 扩展点

1. **新增系统工具**：在 `SystemToolSet.tool_definitions()` 列表中追加一个 `_tool(...)` 定义，并在 `execute()` 中增加对应 `name` 分支（或将 `execute` 改造为按名称分发的 dict 映射）。只要保持 `tool_definitions()` + `execute(...)` 契约不变，新工具即可被 `ToolRegistry` 自动注册，无需改动 `main.py`。
2. **让工具具备依赖**：可参考 `sandbox.SandboxToolSet` 的注入模式（`main.py` 第 67 行 `SandboxToolSet(sandboxes)`），为 `SystemToolSet` 增加构造参数并在 `main.py` 第 81 行调整装配；但需注意模块边界——"需要用户数据的工具必须回到对应功能模块，不能放进这里"（Go 版 README 的约束同样适用）。
3. **新增模块级能力（如 HTTP 路由）**：当前模块无路由层，如需新增需引入 FastAPI router 并在 `main.py` 的 `include_router` 列表挂载（当前未实现）。
