# Agent 工具调用全链路（含本次迭代修改点标注）

> 更新：2026-09-08。标注 🛠️ = 本次迭代（9-04 ~ 9-08）新增/修改；其余为原有架构。
> 说明：所有行号基于当前工作区 `backend-py/` 实际状态，改动后可重新 grep 校准。

---

## 一、链路总览

```
用户消息 → BFF(Next.js) → POST /internal/gateway/messages:stream
  → webadapter   → gateway → agentrun Service.run() → _run()
  → chat() 工具循环（≤8 轮，每轮可执行多个工具）
      → 工具分发: ToolRegistry(ToolRegistry.execute)
          → SandboxToolSet.execute → SandboxService.workspace()
              → E2B SDK（files/commands）→ 结果 JSON 字符串
      → 错误分类/熔断统计 → 结果 append 回 messages → 下一轮
  → run.completed / run.error(熔断) → SSE 事件回前端
```

---

## 二、分阶段详解

### 阶段 1：HTTP 入口 → run 启动

| 环节 | 位置 | 说明 |
|---|---|---|
| SSE 入口 | `webadapter/adapter.py:45` | `POST /internal/gateway/messages:stream`，`stream_message()` |
| 转发 gateway | `webadapter/adapter.py:47` | `self.gateway.run(IncomingMessage(...))` → 返回 `(Stream, Error)` |
| 轮询/取消 | `webadapter/adapter.py:83` / `:90` | `GET /internal/gateway/runs/{id}`、`POST .../cancel` |
| run 主流程 | `agentrun/service.py` | `run()` 校验 → session 锁（lanes）→ `_run()` |
| 事件生命周期 | `agentrun/service.py:172` `_run()` | `run.started` → open MCP → `chat()` → 终态事件 → `finally` 收尾 |

### 阶段 2：chat() 消息组装（execution.py）

| 环节 | 行号 | 说明 |
|---|---|---|
| 常量 | `execution.py:26` | `MAX_TOOL_ROUNDS = 8`（模型调用轮次上限） |
| 熔断常量/异常 | `execution.py:31` / `:35` | 🛠️ `TOOL_FAILURE_CIRCUIT_BREAKER = 3` / `ToolCircuitBreakerError` |
| retryable 解析 | `execution.py:48` | 🛠️ `_parse_result_retryable(result) -> bool\|None` |
| 消息组装 | `execution.py:59` `build_messages()` | system + 摘要 + 历史 + 用户消息 |
| 会话摘要压缩 | `execution.py:130` `summary_compress()` | 历史估算 token > 40% 窗口时生成滚动摘要（原有） |
| chat 入口 | `execution.py:278` `chat()` | 流式主循环 |

### 阶段 3：工具循环与执行（execution.py）

| 环节 | 行号 | 说明 |
|---|---|---|
| 跨轮计数器 | `execution.py:323` | 🛠️ `consecutive_failures: dict[str, int]`（各工具连续系统故障次数） |
| 轮次循环 | `execution.py:331` | `for _round in range(MAX_TOOL_ROUNDS)` |
| 无工具则结束 | `execution.py:388` 附近 | `if not tool_calls: return`（模型主动收尾） |
| 工具参数解析 | `execution.py:419-424` | `json.loads(tc["arguments"])` |
| **工具分发** | `execution.py:425-444` | MCP → `mcp_tools.execute`；builtin → `tool_executor.execute`（即 ToolRegistry）；无执行器 → `retryable=False`（`:442`） |
| 兜底异常 | `execution.py:445-447` | 🛠️ `except` → `tool_error_result(tool_name, exc)`（覆盖 MCP 漏网异常） |
| **熔断统计** | `execution.py:452-460` | 🛠️ 解析 `retryable`；`retryable is False` → 累计，≥3 抛 `ToolCircuitBreakerError`；成功/业务错误/无字段 → 重置 |
| is_error 日志 | `execution.py:461` | 🛠️ `is_error = retryable is False` |
| tool 事件状态 | `execution.py:472` | 🛠️ `status = "failed" if retryable is False else "completed"` |
| 结果回传 | `execution.py:474-476` | `messages.append({"role": "tool", "content": result})` → 下一轮模型调用 |
| 轮次用尽收尾 | `execution.py:478-480` | 8 轮仍调工具 → 存空 assistant 消息 + 正常 return（**已还原为原样**，见备注） |

### 阶段 4：ToolRegistry（builtin 工具统一入口）

| 环节 | 行号 | 说明 |
|---|---|---|
| 常量 | `internal/tools.py:11-12` | 🛠️ `TOOL_FAILURE="tool_failure"` / `COMMAND_ERROR="command_error"` |
| 系统故障描述 | `internal/tools.py:15` | 🛠️ `describe_tool_failure()`（包装"重试无用"提示） |
| 业务异常表 | `internal/tools.py:25` | 🛠️ `_BUSINESS_ERRORS`（FileNotFound/IsADirectory/NotADirectory/Permission → retryable+suggestion） |
| **错误分类入口** | `internal/tools.py:33` | 🛠️ `tool_error_result()`：业务错误→command_error+retryable=True+suggestion；系统故障→tool_failure+retryable=False |
| stderr 识别 | `internal/tools.py:62` / `:72` | 🛠️ `_COMMAND_STDERR_HINTS` / `command_stderr_hint()`（识别"伪成功"） |
| Registry 类 | `internal/tools.py:81` | 🛠️ `execute()` 兜底 try/except → `tool_error_result`；unknown tool → tool_failure |

### 阶段 5：SandboxToolSet（沙箱工具）

| 环节 | 行号 | 说明 |
|---|---|---|
| argv 合并 | `sandbox/tools.py:47` | 🛠️ `_join_argv()`（修复 e2b 2.x `args=` 报错） |
| 父目录列名 | `sandbox/tools.py:84` | 🛠️ `_list_dir_names()`（错误提示用） |
| execute 入口 | `sandbox/tools.py:130` | 统一分发 `_op_{name}` |
| **连接沙箱** | `sandbox/tools.py:148` | `workspace(user_id, session_id, workspace_id)`（🛠️ 透传 workspace_id） |
| **sync 失败** | `sandbox/tools.py:149-159` | 🛠️ `except WorkspaceSyncError` → `command_error + retryable=True + suggestion`（"文件尚未同步，请稍后重试"） |
| 沙箱不可用 | `sandbox/tools.py:160-165` | 🛠️ 补 `retryable=False` |
| 路径补全 | `sandbox/tools.py:167-168` | `_FILE_TOOLS` 工具自动补全 `workspace/{id}/` 前缀 |
| **执行工具** | `sandbox/tools.py:169` | `handler(sandbox, arguments)` |
| **write_file 双写** | `sandbox/tools.py:170-174` | 🛠️ 写沙箱成功后回写本地源（双副本修复，候选 A） |
| 工具异常 | `sandbox/tools.py:177` | 🛠️ `tool_error_result(name, exc)` |
| read_file | `sandbox/tools.py:207` | 🛠️ FileNotFoundError 时附父目录实际内容（纠正 Agent 路径幻觉） |
| write_file | `sandbox/tools.py:231` | 写沙箱 `sandbox.files.write` |
| **run_command** | `sandbox/tools.py:254` | 🛠️ exit_code≠0 或 stderr 命中关键词 → `kind=command_error + retryable + suggestion`（解决"伪成功"） |

### 阶段 6：SandboxService（沙箱生命周期 + 同步）

| 环节 | 行号 | 说明 |
|---|---|---|
| 查映射 | `sandbox/service.py:44` | `_load_sandbox_id()` |
| 只读连接 | `sandbox/service.py:53` | `existing_workspace()`（🛠️ 已去掉 sync，前端面板走本地直读） |
| 本地路径映射 | `sandbox/service.py:69` | `local_workspace_path()`（归属校验 + Local 后端判定） |
| **本地双写** | `sandbox/service.py:88` | 🛠️ `write_local_workspace_file()`（候选 A，仅 Local + workspace/ 路径生效） |
| **按需同步** | `sandbox/service.py:103-119` | 🛠️ `_sync_workspace_files(workspace_id, force)`：仅 `force`（新建）或 `is_dirty` 才同步 |
| 连接/创建沙箱 | `sandbox/service.py:121` | `workspace()`（🛠️ 加 workspace_id 参数） |
| 新建 force 同步 | `sandbox/service.py:173` | 🛠️ create 分支 `force=True` |

### 阶段 7：Volume 同步后端

| 环节 | 行号 | 说明 |
|---|---|---|
| 同步异常 | `volume_backend.py:29` | 🛠️ `WorkspaceSyncError` |
| 抽象方法 | `volume_backend.py:46` | 🛠️ `sync_workspace_to_sandbox(workspace_id, sandbox)`（E2B no-op；替代原 `sync_all_to_sandbox`） |
| Local 实现 | `volume_backend.py:161` | 🛠️ 单 workspace 全量同步（rglob + `write_files`），失败 re-raise `WorkspaceSyncError` |
| dirty 标记 | `volume_service.py:27` | 🛠️ `self._dirty: set[str]` |
| 上传置 dirty | `volume_service.py:48` | 🛠️ `write_files` 写成功后 `self._dirty.add(workspace_id)` |
| 同步清 dirty | `volume_service.py:57-60` | 🛠️ `sync_to_sandbox(sandbox, workspace_id)` 成功后 discard |
| dirty 查询 | `volume_service.py:62` | 🛠️ `is_dirty(workspace_id)` |

### 阶段 8：熔断 → 终态事件

| 环节 | 行号 | 说明 |
|---|---|---|
| 熔断捕获 | `agentrun/service.py:242` | 🛠️ `except ToolCircuitBreakerError` → `run.error(type="tool_circuit_breaker")` |
| 通用异常 | `agentrun/service.py:254` | `run.error(type="internal_error")` |
| 完成事件 | `agentrun/service.py:215` | `run.completed`（chat 正常返回） |

---

## 三、错误处理闭环（本次迭代的核心产物）

```
工具异常/命令结果
  ├─ 业务错误（FileNotFound/权限/命令失败/stderr 命中）
  │    → command_error + retryable=True + suggestion → 模型自救（不熔断）
  │       ├─ 路径错误：附父目录实际内容（read_file 增强）
  │       └─ sync 未完成：明确"请稍后重试"（WorkspaceSyncError）
  ├─ 系统故障（SDK 异常/超时/连接断开）
  │    → tool_failure + retryable=False → 熔断统计（连续 3 次 → run.error）
  └─ 成功 → 正常结果
```

---

## 四、本次迭代修改点汇总（按备份文件）

| 备份 | 文件 | 修改内容 |
|---|---|---|
| `.b6` | `internal/tools.py` / `sandbox/tools.py` / `agentrun/execution.py` / `agentrun/service.py` | 首次：kind 分类 + 熔断 + 工具状态 |
| `.b7` | 同上 3 文件（tools/execution） | 业务错误（BUSINESS_ERRORS）从 tool_failure 拆出 |
| `.b8` | `internal/tools.py` | 结构化错误：retryable + suggestion 字段 |
| `.b9` | `internal/tools.py` / `sandbox/tools.py` | stderr 关键词识别（command_stderr_hint） |
| `.b10` | `volume_backend.py` / `sandbox/service.py` / `sandbox/tools.py` | WorkspaceSyncError + sync 失败显式传播 |
| `.b11` | `sandbox/tools.py` | read_file 错误附父目录内容 |
| `.b12` | 4 文件（volume/sandbox） | 按需同步 + dirty 降频 |
| `.b13` | `sandbox/service.py` / `sandbox/tools.py` | write_file 双写本地（候选 A） |
| `.b14` | `agentrun/execution.py` | 轮次用尽提示（**已还原**，见备注） |

> **备注**：`.b14` 对应的"轮次用尽时给用户追加提示"改动已被用户撤销（`execution.py:478-480` 已用 `.b14` 还原为原样）。当前轮次用尽仍走"存空 assistant 消息 + 正常 return"，无用户提示；待改用"事前给模型轮次预算信号"方案（对齐 Pi 的"限制也是给模型的反馈"哲学）。
