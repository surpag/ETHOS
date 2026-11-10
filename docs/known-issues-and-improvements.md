# 已知问题与改进清单

> 来源：2026-10-19 ~ 2026-10-21 的代码核对、线上事故复盘与面试训练追问。
> 所有条目均有代码位置依据；标「待验证」的是尚未实测确认的推断。
>
> **分级标准**
> - **P0**：会导致数据丢失、任务被误判为完成、或信息不可恢复。
> - **P1**：不会静默丢数据，但会放大延迟/成本、在压力下雪崩、或形成安全弱点。
> - **P2**：可维护性、观测精度、体验优化，不影响正确性。

---

## 一、P0 正确性与数据丢失

### P0-1　「无工具调用即完成」——假完成路径

**现象**：模型这一轮没有发起工具调用，系统判定任务完成，写 `task_status = completed` 并**清空 checkpoint**。用户实际只拿到一句半截话，任务没做完，进度依据也一并消失。

**根因**（两处叠加）：

```python
# agentrun/execution.py:651
if not tool_calls:
    ...
    return usage_acc.tokens(), used_model_id, tool_calls_count, end_reason, duration
```

```python
# agentrun/execution.py:456 —— end_reason 的初值就是 "completed"
end_reason = "completed"
```

```python
# agentrun/service.py:318-322
if end_reason == "completed":
    await self.store.set_task_status(..., "completed")
    await self.store.clear_checkpoints(...)
```

「完成」不是判出来的，是**没人改它**。而同一系统里 `task_status` 的默认方向是「不写 = 没完成」。

**三类真实触发场景**：

| 场景 | 触发条件 | 实际含义 |
|---|---|---|
| 输出被截断 | `finish_reason = "length"`，tool_call 分片一个都没发出 | 模型被打断，不是它决定的 |
| 宣告代替执行 | 模型输出「我先重新读取 a.py 确认后继续」但未发起调用 | 把计划写成了文本 |
| 阶段性总结 | 12 个文件改了 7 个，输出「剩余部分结构相似」即收尾 | 模型不知道还剩多少 |

**影响**：三个信号（UI 显示、`task_status`、checkpoint）同时指向「完成」，且 trace 全绿、日志无 ERROR —— 唯一的可观测证据是用户的体感。

**建议修法（三层，从便宜到彻底）**：

1. **立即**：检查 `finish_reason == "length"` → 不判完成，保持 `in_progress`。成本极低，挡住最好检测的一类。
2. **短期**：`completed` 不再由「无 tool_calls」推定，改为由**显式完成信号**驱动（模型调用 `finish_task` 工具，或返回结构化终止标记）。无信号 → 保持 `in_progress`。
3. **中期**：引入任务清单（见 P1-1），完成 = 清单由非空变为空；且每项核销必须绑定一条成功的、路径匹配的工具调用记录（证据绑定）。

**验证方式**：构造 `finish_reason=length` 的响应，断言 `task_status` 仍为 `in_progress` 且 checkpoint 未被清除。

---

### P0-2　`finish_reason` 全项目从未被读取

**现象**：`grep finish_reason` 在 `agentrun/` 下 0 命中。

**影响**：模型输出被供应商上限截断时，系统完全无感。这不仅是 P0-1 的一个诱因，也意味着**「回复变短」这类线上问题没有任何指标支撑**。

**建议**：采集 `finish_reason` 进 `llm_step.metadata`，在 trace 与用量统计中按 `stop / length / tool_calls` 分布统计。截断率是一个应当被监控的健康指标。

---

### P0-3　摘要实现与注释不一致 → 早期信息直接丢失

**现象**：`execution.py:290` 注释写「把旧摘要 + 全部历史压缩成新摘要」，但 `prompt_messages` 里只有 system（摘要指令）+ user（`请摘要以下对话：{history_text}`），**`old_summary` 没有被拼进 prompt**。

```python
new_summary = response.choices[0].message.content or old_summary   # 旧摘要只当兜底
```

**影响**：不是「信息逐渐稀释」，是**整体覆盖**。第 1 轮定的接口命名、技术选型，如果只存在于旧摘要里，第 30 轮后会彻底消失。

**建议**：把 `old_summary` 拼进 prompt —— 「基于以下已有摘要与新对话，输出更新后的摘要」，才能真正做到增量、不丢。同时补一条测试：构造「摘要已存在 + 新历史不含早期信息」的用例，断言新摘要仍包含早期关键实体。

---

### P0-4　`clear_checkpoints` 会销毁恢复依据

**现象**：`task_status` 写 `completed` 或 `canceled` 时，`checkpoint` 与 `read_progress` 一并删除。

**影响**：用户重新说「继续」时，系统**从零重扫**——每个已读文件重读一次（E2B 单次 2~9s）。前面那套 WAL + 断点续跑 + 读进度的工程量，在这条路径上全部作废。

**建议**：与 P0-1 联动。区分「完成」（可清）与「未确认完成」（保留）；即使保留成本高，也可保留压缩后的摘要版本（只留 step + 已读文件清单，不留全文摘要）。

---

## 二、P1 稳定性与资源

### P1-1　工具并发无上限

**现象**：

```python
# agentrun/execution.py:743
if len(ordered) > 1 and all(_is_readonly_tool(tc["name"]) for tc in ordered):
    executed = await asyncio.gather(*[_run_one(tc) for tc in ordered])
```

`asyncio.gather` 没有信号量。模型一次发 20 个 `sandbox_read_file` → 20 个请求同时打向 E2B。

**三层后果**：

1. **对端**：E2B 沙箱是单容器，文件服务并发能力有限 → 排队/限流。注意「单次 2~9s」是**独占**下的数字，并发下会显著恶化。
2. **超时雪崩**：E2B SDK 默认 **60s 整请求硬超时**；排队超时 → 抛异常 → 包装成 `retryable` 工具错误 → 模型以为路径错 → 换策略重试 → 再一批并发。
3. **上下文**（最致命）：20 个文件内容同时进 `messages` → 下一轮 `_estimate_tokens > 0.8 × context_window` → `end_reason = "context_budget_exhausted"`，run 当场收尾。

**建议**：

- 工具执行层加信号量（`asyncio.Semaphore`，建议 4~6），**不要加在循环层**——循环层管「跑几轮」，管不了「一轮里同时发几个」。
- **按字节而非个数限制**：一批读入总字节上限（如 2MB）+ 单文件上限。20 个小文件并发无害，3 个 10MB 文件是灾难。优先级：字节限制 > 并发数限制。
- 超时错误应携带明确的「系统过载」语义，而不是伪装成业务错误——**错误的呈现形式决定模型的反应是否正确**。

---

### P1-2　工具结果体积无上限

**现象**：`_trim_tool_results(keep_recent=6, limit=1500)` 只截断**较早**的工具结果（保留最近 6 条全文），最近一条可能是几十万字符的大文件。`EDITH_SYNC_MAX_FILE_BYTES` 只约束同步层，不约束模型上下文。

**建议**：单条工具结果进 `messages` 前做体积上限（如 64KB）+ 截断标记；超限时提示模型「用 offset/limit 分段读」，而不是静默截断。

---

### P1-3　截断的工具结果会诱发假完成

**现象**：被截断的结果末尾是「…（较早的工具结果已截断，原 8000 字符）」。模型意识到内容不全后，常见反应是输出「我先重新读取确认」而**不发起调用** → 直接命中 P0-1。

**建议**：截断标记改为可执行指令（「内容已截断，需要完整内容请调用 sandbox_read_file(path, offset)」），降低「说而不做」的概率。治本仍在 P0-1。

---

### P1-4　checkpoint 不能作为「做了什么」的证据

**现象**：

```python
# agentrun/execution.py:78
def _digest(value: str, limit: int = 200) -> str:
    """短则原样，长则截断 + sha256 前缀 + 长度。"""
```

- `args_digest` / `result_digest` 只保留 200 字符 + 哈希 → 无法还原真实参数与结果；
- **`result_digest` 不区分成功与失败** —— 一次 `{"error": ..., "retryable": true}` 同样写一条 checkpoint；
- `run_checkpoints` 表**没有 `task_item_id`** → 无法表达「这次调用属于哪个任务项」。

**影响**：想做「证据绑定」（P0-1 第 3 层修法）时，现有 checkpoint 支撑不了。

**建议**：新增 `status` 字段（success / failure）+ 可选 `task_item_id`；`args_digest` 保留完整结构中的关键字段（如 `path`）而非纯截断。

---

### P1-5　只读白名单只有两个工具，保守但过度

**现象**：`_READ_ONLY_TOOLS = frozenset({"sandbox_read_file", "sandbox_list_files"})`。`git status`、`sandbox_run_command("ls")` 一律按写操作串行。

**评估**：**保守方向正确**（未知默认串行，fail-safe），但代价是一个写操作拖垮整批——10 读 + 1 写 = 11 个全串行。

**不建议**改为细粒度依赖分析，原因：`run_command` 副作用不可静态分析、路径参数解析不出真实范围（通配符/符号链接/相对路径）、判错的代价是不可逆的数据不一致。**代价不对称，保持保守。**

**可选优化**：把 `git` 的只读子命令（status / log / diff）单独建模为只读工具，扩大白名单——但要先确认其副作用边界。

---

### P1-6　`secret.py` 是混淆不是加密

**现象**：`XOR + Base64`（`enc:v1:` 前缀），密钥与密文同库。

**影响**：云端多租户已上线，获取 DB 文件即可还原用户 API Key / Git PAT。

**建议**：迁到 KMS / 信封加密（DEK 加密数据、KEK 存 KMS）；至少做到密钥不与密文同存储。

---

## 三、P2 可维护性与观测

### P2-1　`end_reason` 与 `task_status` 默认方向相反

`task_status`：不写 = 没完成（悲观）。`end_reason`：不改 = 完成（乐观）。两个默认值最终汇合到同一条判定链上，是 P0-1 的结构性根因。建议统一表述：**「完成」必须是正面证据，不能是缺省值。**

---

### P2-2　`_extract_and_save_memories` 未计入用量

`agentrun/service.py` finally 块内、`usage.finish()` 之后的那次模型调用未计量 → 会话成本被系统性低估。建议在 `finish` 之前完成，或单独记一笔。

---

### P2-3　同步的免下载比对只看 size

`workspace/sync.py`：远端 `files.list` 的 size 与本地一致即不读内容 → **同长度改写不会被同步**。已知取舍，建议在文档中显式声明，并在 `sync` 日志里记录「跳过比对的文件数」以便估算偏差。

---

### P2-4　`run_command rm` 绕过工具层，可能残留空父目录

工具层删除会回收空父目录（`_prune_empty_parents`，且遵守「先 list 确认为空」）；但 Agent 通过 `run_command rm -rf` 删除时不会触发回收。无数据损失，仅目录结构不洁。

---

## 四、部署运维

### OPS-1　`deploy/nginx.conf` 缺 `client_max_body_size`

nginx 默认 1MB，工作区文件上传超过 1MB 直接被 413。`nginx.https.conf` 里写了 `100m`，但线上挂载的是 `nginx.conf`。

### OPS-2　`proxy_set_header Connection "";` 会导致 WebSocket 握手失败

对 SSE 无影响（`proxy_read_timeout 3600s` 已放长），但第二个应用若用 WebSocket 必挂。多应用部署时需改为 `map $http_upgrade $connection_upgrade`。

### OPS-3　`server_name _` 是顺序依赖的默认站点

`deploy/nginx.conf:15` 是唯一的 catch-all 块，靠「排在第一位」成为默认站点。新增第二个 `server` 块若写在前面，未匹配的 Host 会静默落到新应用。**必须显式标 `default_server`。**

### OPS-4　多应用共用 `edge` 网络时，service 名不能重名

两个 compose stack 都叫 `web` 时，共享网络内 DNS 同名 → nginx 轮询解析 → 「偶尔打开是另一个应用」，不报错、难排查。

---

## 五、外部依赖阻塞

### EXT-1　E2B Volume 未开通（403: use of volumes is not enabled）

Volumes 目前是 private beta，需向 support@e2b.dev 申请。当前 `.env` 走 `EDITH_WORKSPACE_VOLUME_BACKEND=local`，因此 **B1–B3 同步层仍是活跃路径**（删除传播、性能护栏都在生效）。开通后切 `e2b` 可让同步层退化为可选。

---

## 六、建议的施工顺序

| 顺序 | 条目 | 理由 |
|---|---|---|
| 1 | P0-1 第 1 层（`finish_reason` 检查） | 改动最小、挡住最易检测的一类假完成 |
| 2 | OPS-1 / OPS-2 | 一行配置，线上立刻受益 |
| 3 | P1-1（字节上限 + 信号量） | 直接决定长任务能否跑完 |
| 4 | P0-3（旧摘要拼进 prompt） | 修一处漏一处信息，成本低收益高 |
| 5 | P0-1 第 2/3 层（完成信号 / 任务清单） | 需要设计，建议单独立项 |
| 6 | P1-6（凭据加密） | 多租户已上线，属合规级 |

---

## 附：本清单未覆盖的部分

以下方向在训练中被识别，但尚未形成具体条目，属于「下一步该想清楚」的范畴：

- 资源公平与成本隔离（单用户跑满并发时如何不影响他人）
- 提示注入的纵深防御（工具描述、文件内容、MCP 返回都可能携带指令）
- 审计日志的可信度（谁能改、能否被 Agent 触碰）
- 摘要漂移的度量（压缩质量如何评估，而不只是「压缩了多少 token」）
- 不可逆动作的撤销与预演（删除/推送前的 dry-run）
