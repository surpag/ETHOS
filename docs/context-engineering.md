# 上下文工程（Context Engineering）现状分析与优化路线

> 状态：规划稿（**未实施**，作为未来优化参考）
> 目标：梳理当前项目上下文工程的实现现状，对照主流 AI 编程助手，明确短板与优化优先级。
> 定位：本文档是「未来要优化时再读」的路线图，不描述已完成的改动。

---

## 一、背景

上下文工程是 AI 编程助手的核心竞争力，核心方向围绕两个维度：

1. **输入侧**：工具输出截断、动态加载、Token 预算控制——控制"喂给模型的东西有多大"。
2. **历史侧**：Compaction（压缩/摘要）、上下文隔离——控制"历史对话占用多少窗口"。

主流产品（Codex / Cursor / Copilot / DeepSeek Harness / Claude Code）在核心方向上高度一致，本文档对照这些产品，分析本项目的差距与可改进之处。

---

## 二、当前项目上下文工程实现盘点

### 2.1 输入侧：工具输出截断

代码位置：`backend-py/src/edith_backend/internal/sandbox/tools.py`

| 能力 | 现状 |
|---|---|
| 读文件截断 | `DEFAULT_READ_LIMIT = 32 * 1024`（32KB，**按字节**截断），`read_file` 有 `offset`/`limit` 参数可分段续读 |
| 命令输出截断 | `MAX_COMMAND_OUTPUT = 32 * 1024`（32KB），`_shorten()` 截断 stdout/stderr |
| 截断标志 | 返回 `truncated` / `stdoutTruncated` 字段 |
| **spill（输出转文件）** | ❌ **无**——截断内容直接丢弃，模型无法回溯剩余部分 |
| **Token 截断** | ❌ 无——纯字节截断，非基于 Token |
| 工具描述引导 | ⚠️ `read_file` 有 offset/limit 参数，但描述未明示"输出已截断、可用 offset 续读" |

### 2.2 历史侧：Compaction（摘要压缩）

代码位置：`backend-py/src/edith_backend/internal/agentrun/execution.py`（`summary_compress`）

| 能力 | 现状 |
|---|---|
| 摘要触发 | ✅ 已实现，`summary_compress()` |
| Token 估算 | ⚠️ `sum(len(content) // 4)` —— **字符数÷4 粗估，非真实 token 计数** |
| 触发阈值 | `context_window × 0.4`（`HISTORY_THRESHOLD_RATIO=0.4`），且 `len(history) > 10` |
| 摘要生成 | 模型生成，`max_tokens=500`，`summarized_until_id` 单调推进 |
| 防死锁 | ✅ 摘要请求溢出→裁剪最旧一半重试（`SUMMARY_MAX_RETRIES=3`）；摘要超长→严格指令重试（`SUMMARY_MAX_CHARS=2000`） |
| 执行方式 | ⚠️ **同步阻塞**在发消息前执行，非后台异步 |
| TokenMeter 精确计数 | ❌ 无 |
| 按模型独立压缩策略 | ❌ 无（thresholdRatio/retainRatio 思路） |
| 完整历史可查询文件 | ❌ 无（Cursor 的做法） |

### 2.3 系统提示词动态组装与上下文隔离

代码位置：`backend-py/src/edith_backend/internal/agentrun/configuration.py`

| 能力 | 现状 |
|---|---|
| 分层指令（AGENTS.md 等价） | ✅ 工作区指令 + 自动记忆，经 `WorkspaceMemoryStore.load_injectable` 注入（≤200 行 `INJECT_LIMIT`） |
| Skill 摘要注入 | ✅ 只注入 name+description，不含 body |
| **子 Agent（sub-agent）** | ❌ 无 |
| **动态加载 MCP 工具** | ❌ 全量加载（`service.py:195 open_tools` 一次性注入所有工具定义） |

---

## 三、业界实践对照（输入侧 & 历史侧）

### 3.1 输入侧：工具输出截断与动态加载

| 产品 | 做法 |
|---|---|
| Codex | 早期按行数截断（前128行+后128行），社区推动转向 Token 限制（如 25k）；用 AGENTS.md 规则约束工具输出大小（如限制测试日志） |
| Cursor | "输出转文件"策略：长工具响应写文件，模型只拿路径+预览（tail 命令）；动态加载所需 MCP 工具，避免一次性加载全部定义 |
| GitHub Copilot | 默认 >20 KiB 输出存临时文件，模型只接收路径+预览；阈值可用 `COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES` 调整 |
| DeepSeek Harness (DSH) | spill：超大文本存外部文件，只给预览和读取位置；"极简模式"把上下文从 1 万多 Token 压到 1000 多 |

### 3.2 历史侧：Compaction 与上下文隔离

| 产品 | 做法 |
|---|---|
| Codex | Auto-Compaction，接近限制（如 90 万 Token）时自动压缩为结构化摘要 |
| Claude Code | Auto-Compaction，`/autocompact` 命令或 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 调整阈值 |
| Cursor | 窗口填满触发总结，提供工作摘要新窗口；**完整对话历史也存为可查询文件**，摘要后仍能回溯细节 |
| DeepSeek Harness (DSH) | Compaction 阈值 80% 时把最老一段浓缩；`TokenMeter` 估算当前用量；按模型配置独立压缩策略（thresholdRatio / retainRatio） |
| GitHub Copilot | 达到窗口约 80% 时后台自动压缩，留 20% 缓冲 |

### 3.3 系统提示词动态组装与上下文隔离

- **分层指令**：Codex（AGENTS.md）、Claude Code（CLAUDE.md）支持项目/子目录级别的动态分层指令。
- **子 Agent**：DSH、Cursor 支持把特定任务（代码搜索、测试）委派给子 Agent，子 Agent 有独立上下文窗口，保持主会话精简。

---

## 四、对比表

| 能力 | Codex | Cursor | Copilot | DSH | Claude Code | **本项目现状** |
|---|---|---|---|---|---|---|
| 工具输出截断 | 行数→Token(25k) | 输出转文件(spill) | >20KiB存临时文件 | spill+极简模式 | — | ⚠️ 字节32KB，无spill |
| Token 精确计数 | — | — | — | ✅ TokenMeter | — | ❌ `//4` 粗估 |
| Compaction | ✅ 90万tok触发 | ✅ 窗口满触发+存全文 | ✅ 80%后台压缩 | ✅ 80%+独立策略 | ✅ autocompact可调 | ✅ 有(40%阈值) |
| 后台/异步压缩 | — | — | ✅ 后台 | — | — | ❌ 同步阻塞 |
| 压缩阈值可调 | — | — | ✅ env | ✅ threshold/retain | ✅ env+命令 | ⚠️ 硬编码0.4 |
| 完整历史可查询 | — | ✅ 存文件 | — | — | — | ❌ |
| 子 Agent | — | ✅ | — | ✅ | — | ❌ |
| 动态加载 MCP | — | ✅ | — | — | — | ❌ 全量 |
| 分层指令(AGENTS.md) | ✅ | — | — | — | ✅ CLAUDE.md | ✅ 工作区指令 |

**结论**：本项目**历史侧已有 Compaction（四家里较完整的）**，主要短板集中在**输入侧（Token 截断 + spill）**和**上下文隔离（子 Agent、动态 MCP）**，以及**粗估 token 这一基础短板**。

---

## 五、优化建议（按优先级）

### P0 — 修掉粗估 Token（地基问题，收益最大）

**问题**：`sum(len(content) // 4)` 对中文**严重低估**（中文 1 字 ≈ 0.6~1 token，`//4` 把 4 字符算 1 token），导致摘要触发**过晚**，中文长对话可能直接溢出。

**建议**：接入 tiktoken 或按语言分估：

```python
def estimate_tokens(text: str) -> int:
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.25)
```

> 这是本项目与 DSH 的 **TokenMeter** 之间最核心的差距，直接关系到压缩阈值是否踩得准。

### P1 — 输入侧：引入 spill（输出转文件）

对齐 Copilot/Cursor 的做法，把超长工具输出写到文件、只给模型路径+预览：

- 后端 `run_command`/`read_file` 输出超阈值时，写 `work/` 或 `artifacts/` 临时文件，返回 `{path, preview(前N行), spilled: true}` 而非 `{content截断}`。
- 工具描述补一句"输出过大时会写入文件并返回路径，可用 read_file 分段读取"。

**收益**：命令输出从"32KB 硬截断丢尾"变成"可回溯"，尤其对测试日志、构建报错这类长输出。

### P2 — 上下文隔离：子 Agent + 动态 MCP

- **子 Agent**（对标 DSH/Cursor）：把"代码搜索/全仓扫描"这类吃上下文的任务委派给独立上下文的子 Agent，主会话只拿结论。项目已有 E2B 沙箱 + 模块化工具，加一个 `delegate(subtask)` 工具即可起步。
- **动态 MCP**（对标 Cursor）：`open_tools` 改为按需加载（先注入工具名称+一句话描述，模型明确要用时再拉完整 schema），避免几十个 MCP 工具定义撑爆首轮上下文。

### P2 — 压缩策略精细化

- `HISTORY_THRESHOLD_RATIO` 从硬编码改为**按模型配置**（DSH 的 thresholdRatio/retainRatio 思路），大窗口模型 0.6、小窗口 0.3。
- 摘要触发改为**后台异步**（Copilot 做法），避免同步阻塞首轮响应。

---

## 六、实施顺序建议

1. **P0 先做**：Token 估算修正，改动最小、收益最直接（一行函数替换）。
2. **P1 次之**：spill 机制，前后端都要动（约 30 行）。
3. **P2 最后**：子 Agent / 动态 MCP / 压缩策略精细化，属架构级扩展，需要专门设计。

---

## 附：关键代码位置索引

| 功能 | 文件 | 位置 |
|---|---|---|
| 读文件截断 | `internal/sandbox/tools.py` | `DEFAULT_READ_LIMIT`、`_op_sandbox_read_file` |
| 命令输出截断 | `internal/sandbox/tools.py` | `MAX_COMMAND_OUTPUT`、`_shorten` |
| 摘要压缩 | `internal/agentrun/execution.py` | `summary_compress`、`HISTORY_THRESHOLD_RATIO` |
| 消息组装 | `internal/agentrun/execution.py` | `build_messages` |
| 系统提示词组装 | `internal/agentrun/configuration.py` | `load`、`run_instruction`、`skill_instruction` |
| MCP 工具加载 | `internal/agentrun/service.py` | `open_tools` |
