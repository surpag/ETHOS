# 会话摘要 / 历史压缩方案（方向 2）

> 状态：设计稿（未实施）
> 目标：长对话时把早期消息折叠成摘要，控制 Token 消耗、避免上下文窗口溢出。
> 背景：Go 版有 `session_summary.go`（依赖 trpc-agent-go 框架的摘要器），Python 版手写编排重写时遗漏。

---

## 一、现状与痛点

**Python 版现状**（`internal/agentrun/execution.py`）：

```python
def build_messages(system_instruction, history, user_message):
    messages = [{"role": "system", "content": system_instruction}]
    messages.extend(history)                      # ← 全量历史，无压缩
    messages.append({"role": "user", "content": user_message})
    return messages
```

- `store.get_messages()` 返回**全量** `[{role, content}]`；
- `chat_messages` 表无摘要字段；
- `ModelDefinition.context_window`（如 1M / 256K）已存在，但**未用于历史压缩**。

**痛点**：长对话时 Token 消耗线性增长；超过模型上下文窗口会请求失败或被截断；历史前缀变化导致缓存命中率下降。

## 二、Go 版原语义（对齐参照）

`backend-v2/internal/agentrun/session_summary.go`：

- **触发阈值**：新增事件达到当前模型上下文窗口的 **40%**（`sessionSummaryContextRatio = 0.4`）；
- **摘要模型**：复用本次运行的模型（`invocation.Model`），API Key 只从 invocation 读取、不落盘；
- **行为**：把早期消息折叠成一段摘要，后续请求用「摘要 + 近期消息」。

## 三、方案设计（最小，不依赖框架）

### 1. 存储：新增会话摘要表

`internal/session/schema.py` 增加一张表：

```sql
CREATE TABLE IF NOT EXISTS session_summaries (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summarized_until_id INTEGER NOT NULL,   -- 已折叠到的最后一条消息 id
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, session_id)
)
```

`summarized_until_id` 记录「已经折叠进摘要的最后一条消息 id」，`id > summarized_until_id` 的消息即「近期消息」。

### 2. 触发判断（在 `chat()` 组装消息前）

```python
HISTORY_THRESHOLD_RATIO = 0.4   # 对齐 Go 版

# 粗估历史 token：按字符数折合（中文约 1.5~2 字符/token，取 4 字符/token 偏保守，稍早触发无妨）
estimated_tokens = sum(len(m["content"]) for m in history) // 4
threshold = int(configured.definition.context_window * HISTORY_THRESHOLD_RATIO)
```

- 若 `estimated_tokens <= threshold`：走现有全量历史逻辑，**不摘要**；
- 若超过：进入下面的摘要流程。

### 3. 摘要生成（复用本次运行的模型）

复用 `chat()` 里已有的 `AsyncOpenAI client` 和 `configured.definition.api_model`，发一次额外的摘要调用：

```
system: "你是对话摘要助手，请把用户与助手的对话压缩成简洁的要点摘要，保留关键事实、用户目标与未完成事项。"
user:   待摘要的消息拼接文本
```

**关键约束**（对齐 Go 版安全语义）：
- API Key 复用 `configured.api_key`，**摘要文本不写入密钥、也不把密钥拼进待摘要内容**；
- 摘要是一次**额外的模型调用**，会消耗少量 token 和延迟——这是「省钱」的代价，但只在超阈值时发生。

### 4. 消息组装（摘要 + 近期）

摘要完成后，更新 `session_summaries`，然后组装：

```python
messages = [{"role": "system", "content": system_instruction}]
if summary:
    # 摘要作为 system 的追加段（保持 role 语义干净）
    messages[0]["content"] += "\n\n【之前对话摘要】\n" + summary
messages.extend(recent_history)          # 仅 id > summarized_until_id 的消息
messages.append({"role": "user", "content": user_message})
```

### 5. 触发流程时序

```
chat() 开始
  → get_messages() 拿全量历史
  → 读 session_summaries 得到 summarized_until_id + summary
  → recent = history[id > summarized_until_id]
  → 估算 recent 的 token
  → 若 > context_window * 0.4：
       用模型把「旧 summary + recent 的前半段」折叠成新 summary
       更新 summarized_until_id + summary
  → 组装：system(含摘要) + recent + user_message
```

## 四、最小改动清单（4 个文件）

| 文件 | 改动 |
|------|------|
| `internal/session/schema.py` | + `session_summaries` 建表语句 |
| `internal/session/store.py` | + `get_summary` / `save_summary` 两个方法 |
| `internal/agentrun/execution.py` | + 触发判断 + 摘要生成 + 组装逻辑（`chat()` 内） |
| `internal/agentrun/execution.py`（或新 `summary.py`） | 摘要 prompt 与调用封装 |

> 建议把摘要逻辑抽成 `execution.py` 内的一个独立函数 `_maybe_summarize(client, configured, store, ...)`，不污染主流程。

## 五、边界与权衡

| 项 | 说明 |
|----|------|
| **token 估算精度** | 用字符数粗估，非精确 tokenizer。偏保守（稍早触发）可接受，避免引入额外依赖 |
| **摘要失败降级** | 摘要调用失败时**放弃摘要、退回全量历史**，保证对话可用（可打 warning 日志） |
| **首次摘要延迟** | 超阈值那次会多一次模型调用（约 1~3s），只发生在长对话首次触发时 |
| **摘要质量** | 摘要会丢失部分细节，这是成本换取的必然取舍；阈值 40% 意味着近期 60% 上下文仍完整保留 |
| **多轮摘要** | `summarized_until_id` 单调递增，旧摘要 + 新的近期消息合并再摘要，避免历史无限膨胀 |
| **图片消息** | 摘要只折叠文本，图片消息（image_ids）的近期消息仍原样保留 |

## 六、验证方式

1. 用一个短上下文窗口的模型（或临时把阈值调低到 `0.1`）连发多轮消息，观察：
   - 首次触发时日志出现摘要调用（可复用方向 1 的日志，加一条 `summary triggered`）；
   - `session_summaries` 表出现记录，`summarized_until_id` 递增；
2. 检查每次请求的 `prompt_tokens`（方向 1 的 end 日志已有 tokens）不再随轮次线性增长，而是趋于平缓；
3. 摘要失败时（临时断网/改错 api_model）确认对话仍能正常返回（降级到全量历史）。
