# Agent 工具错误语义与熔断机制（详细设计）

> 状态：设计稿（**待实施**）
> 背景：2026-09-04 `sandbox_run_command` 因 E2B SDK 签名漂移全部报 `args` TypeError，模型 24 分钟空转 8 轮后 run 被标成 `run.completed`。
> 参照：Pi Agent Book ch05「错误即消息」哲学 + 修正（补齐 Pi 缺失的「错误分类」层 + 模型失控时的兜底熔断）。

---

## 1. 目标

1. 区分两类工具错误，让模型能正确判断「换命令能否解决」。
2. 系统故障时给模型**明确、具体、可行动**的提示，让它主动停手（优先靠模型自救）。
3. 模型失控（无视提示、连续撞同一系统故障）时，框架兜底终止 run 并明确报错，而不是空转到 `completed`。

---

## 2. 设计原则

1. **错误即消息**：工具异常不穿透，统一翻译成带 `kind` 的消息回传模型（沿用现有 `except → {"error": ...}` 模式）。
2. **分类是地基**：Pi 缺的一层——没有分类，模型分不清「命令错了」和「工具坏了」。
3. **具体描述 > 裸透传**：系统故障要包装成「是什么错 / 为什么 / 怎么办」，不裸传 `str(exc)`。
4. **兜底熔断**：只在模型失控时触发（常态靠模型自救，异常靠保险丝）。

---

## 3. 错误分类契约

常量定义在 `internal/tools.py`（工具层与编排层共用）：

```python
TOOL_FAILURE = "tool_failure"     # 工具系统故障：SDK 异常 / 连接断开 / 超时 / 参数错误
COMMAND_ERROR = "command_error"   # 命令执行了但业务失败：exit_code != 0
```

| kind | 触发 | 描述策略 | 参与熔断 |
|---|---|---|---|
| （无） | 成功（exit_code==0、文件操作成功） | 正常结果 | 否 |
| `command_error` | 命令非零退出 | 保留 exitCode/stderr，模型自救 | 否 |
| `tool_failure` | 工具系统故障 | `describe_tool_failure` 包装（含「重试无用」提示） | **是** |

---

## 4. 模块改动明细

### 4.1 `internal/tools.py`（常量 + 描述函数 + ToolRegistry 兜底）

```python
import json

TOOL_FAILURE = "tool_failure"
COMMAND_ERROR = "command_error"


def describe_tool_failure(tool_name: str, exc: Exception) -> str:
    """把工具系统故障包装成模型可理解、且明确提示「重试无用」的描述。"""
    return (
        f"工具 {tool_name} 执行时发生系统故障：{exc}。"
        "这不是你的命令或参数问题，重试无法解决，请停止调用该工具并向用户说明故障。"
    )


class ToolRegistry:
    async def execute(self, name, arguments, user_id, session_id, workspace_id=""):
        tool_set = self._by_name.get(name)
        if tool_set is None:
            return json.dumps(
                {"error": f"unknown tool: {name}", "kind": TOOL_FAILURE},
                ensure_ascii=False,
            )
        try:
            return await tool_set.execute(name, arguments, user_id, session_id, workspace_id)
        except Exception as exc:  # 兜底：任何工具未处理的异常 → tool_failure
            return json.dumps(
                {"error": describe_tool_failure(name, exc), "kind": TOOL_FAILURE},
                ensure_ascii=False,
            )
```

### 4.2 `sandbox/tools.py`（补 kind）

顶部加 import：

```python
from ..tools import TOOL_FAILURE, COMMAND_ERROR
```

`execute` 内两处 `except`（E2B 不可用降级 / 工具错误）补 `kind`：

```python
# except 1（原 :124-126）E2B 不可用降级 —— 保留「转纯文本」文案，但仍是系统故障，纳入熔断
return json.dumps({"error": f"sandbox unavailable: {exc}", "kind": TOOL_FAILURE}, ensure_ascii=False)

# except 2（原 :132-133）工具错误
return json.dumps({"error": str(exc), "kind": TOOL_FAILURE}, ensure_ascii=False)
```

`_op_sandbox_run_command` 返回处：非零退出加 `command_error` 标记（**保留 exitCode/stderr 结构，不套 error 外壳**）：

```python
resp = {"exitCode": result.exit_code or 0, "stdout": stdout, "stderr": stderr,
        "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}
if resp["exitCode"] != 0:
    resp["kind"] = COMMAND_ERROR
return resp
```

### 4.3 `agentrun/execution.py`（熔断 + 兜底 + 工具事件状态）

顶部新增：

```python
from ..tools import TOOL_FAILURE, describe_tool_failure

TOOL_FAILURE_CIRCUIT_BREAKER = 3   # 同工具连续 3 次 tool_failure 熔断


class ToolCircuitBreakerError(Exception):
    def __init__(self, tool_name: str, failures: int, last_error: str):
        self.tool_name = tool_name
        self.failures = failures
        self.last_error = last_error
        super().__init__(
            f"工具 {tool_name} 连续 {failures} 次系统故障，已熔断停止。最后一次错误：{last_error}"
        )


def _parse_result_kind(result: str) -> str | None:
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    return data.get("kind") if isinstance(data, dict) else None
```

`chat()` 顶部（`for _round` 循环外）声明跨轮计数器：

```python
consecutive_failures: dict[str, int] = {}
```

工具执行处（原 :395-413）加兜底（覆盖 MCP 等漏网异常）：

```python
try:
    if is_mcp:
        result = await mcp_tools.execute(tc["name"], arguments)
    elif tool_executor is not None:
        result = await tool_executor.execute(tc["name"], arguments, user_id, session_id,
                                             getattr(configured.request, "workspace_id", "") or "")
    else:
        result = json.dumps({"error": "no tool executor", "kind": TOOL_FAILURE}, ensure_ascii=False)
except Exception as exc:
    result = json.dumps(
        {"error": describe_tool_failure(tool_name, exc), "kind": TOOL_FAILURE},
        ensure_ascii=False,
    )
```

结果处理处（原 :417-429）加熔断统计 + 修正工具状态：

```python
kind = _parse_result_kind(result)
if kind == TOOL_FAILURE:
    consecutive_failures[tool_name] = consecutive_failures.get(tool_name, 0) + 1
    if consecutive_failures[tool_name] >= TOOL_FAILURE_CIRCUIT_BREAKER:
        raise ToolCircuitBreakerError(tool_name, consecutive_failures[tool_name], result)
else:
    consecutive_failures[tool_name] = 0   # 成功 / command_error / 无 kind 都重置

is_error = kind == TOOL_FAILURE          # 顺带让日志判断更精确
status = "failed" if kind == TOOL_FAILURE else "completed"
queue.put_nowait(decoder.tool_finished(tc["id"], status, result))
```

要点：
- **「连续」跨轮累计**：模型通常下一轮才重试，计数器放循环外，只有成功/command_error 才清零。
- 熔断抛出后本地 `messages`（未持久化的 assistant/tool 消息）随异常丢弃，无脏历史。

### 4.4 `agentrun/service.py`（熔断 → run.error）

`_run` 的 `except Exception` 之前加显式分支：

```python
except ToolCircuitBreakerError as exc:
    reason = "error"
    await self.usage.fail(request.request_id)
    queue.put_nowait(
        Event(
            type="run.error",
            request_id=request.request_id,
            session_id=request.session_id,
            error=APIError(type="tool_circuit_breaker", message=str(exc)),
        )
    )
```

前端收到 `run.error`（而非 `run.completed`），`error.type=tool_circuit_breaker` 可供前端展示「任务因工具故障中止」。

---

## 5. 数据流（三条路径）

**成功**
```
模型调 sandbox_run_command("ls") → exit_code 0 → 无 kind
→ consecutive_failures 重置 → tool.finished(completed) → 模型继续
```

**command_error（模型自救，不熔断）**
```
模型调 sandbox_run_command("sed -i ... 不存在的文件") → exit_code≠0 → kind=command_error
→ 不计数 → tool.finished(completed) → 模型看到 exitCode/stderr，换命令重试
```

**tool_failure（提示后模型主动停）**
```
模型调 sandbox_run_command → SDK TypeError → kind=tool_failure
→ 描述「这是系统故障，重试无用，请告知用户」 → consecutive=1 → tool.finished(failed)
→ 模型读到提示，停止重试，向用户说明
```

**tool_failure（模型失控 → 兜底熔断）**
```
连续 3 轮模型都调 sandbox_run_command 且都 tool_failure
→ 第 3 次后 consecutive=3 ≥ 3 → raise ToolCircuitBreakerError
→ service 捕获 → run.error(type=tool_circuit_breaker) → 前端显示任务中止
```

---

## 6. 常量与配置汇总

| 常量 | 位置 | 值 | 是否可配 |
|---|---|---|---|
| `TOOL_FAILURE` / `COMMAND_ERROR` | `internal/tools.py` | 字符串 | 否 |
| `TOOL_FAILURE_CIRCUIT_BREAKER` | `execution.py` | `3` | 暂否（有调参需求再环境变量化） |

---

## 7. 迁移步骤（每步可独立验证）

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | `internal/tools.py` 加常量 + `describe_tool_failure` + ToolRegistry 兜底 | pytest 不回归 |
| 2 | `sandbox/tools.py` 两处 except + run_command 加 kind | 单测：异常→tool_failure、非零退出→command_error |
| 3 | `execution.py` 熔断常量/异常/解析/统计/兜底/状态 | mock 工具连败 3 次断言抛 ToolCircuitBreakerError；status=failed |
| 4 | `service.py` except 分支 | 集成：熔断后事件序列 run.error，reason=error |

---

## 8. 测试策略

1. `describe_tool_failure` 单测：输出含工具名、原始错误、「重试无用」提示。
2. `_parse_result_kind` 单测：正常 JSON / 非 JSON / 无 kind 字段 / 非 dict。
3. `SandboxToolSet`：mock `commands.run` 抛异常 → `kind=tool_failure`；返回 `exit_code=1` → `kind=command_error`；`exit_code=0` → 无 kind。
4. 熔断：mock 工具连续返回 `tool_failure`，断言第 3 次抛 `ToolCircuitBreakerError`；中间夹一次 `command_error` 则计数重置。
5. `service._run`：mock chat 抛 `ToolCircuitBreakerError`，断言事件序列以 `run.error(type=tool_circuit_breaker)` 结束、reason=error。

---

## 9. 边界与待确认

1. **N 取值**：`3`（硬编码常量），有调参需求再环境变量化。
2. **command_error 格式**：保留 `exitCode + stderr` 结构、只加 `kind`（模型需要 stdout/stderr 决定下一步）。
3. **`sandbox unavailable` 例外**：保留「转纯文本」降级文案（不套「重试无用」），但 `kind` 仍 = `tool_failure`（纳入熔断统计）。
4. **`describe_tool_failure` 的提示文案**：会作为 tool result 回传给模型，措辞需谨慎（避免误导模型把系统故障当命令问题）。
5. **熔断只统计 tool_failure，不统计 command_error**：command_error 是模型可自救的「合理重试」，不触发熔断。
