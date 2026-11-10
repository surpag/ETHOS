# 待优化清单 V2（结合腾讯 CSIG 面试复盘重新生成）

> 来源：2026-10-29 腾讯 CSIG 面试复盘（17 题专项） × 既有 `docs/known-issues-and-improvements.md`（V1，19 条）× `docs/context-engineering.md` × `docs/degradation-plan.md` × `docs/trace-eval-design.md`。
> 本清单**排除 RAG 相关内容**（面试中的第 10–13 题及 RAG 评测体系不纳入，因其不在本项目主线）。
>
> **重新生成的核心变化**：V1 按「正确性 / 稳定性 / 可维护 / OPS」分层，是**代码 bug 视角**；本版按**面试暴露的能力短板**重新组织，是**"证据 / 验证 / 观测"主线视角**。面试总评 7.5/10，最需要提升的不是"背名词"，而是"把理解组织成有证据、有排查路径、能经受追问的工程回答"——这与项目里已识别的一批真实缺陷高度同源。

---

## 施工进度（2026-10-29 更新）

| 项 | 状态 | 落地内容 | 验证 |
|---|---|---|---|
| A1 `finish_reason` | ✅ 完成 | 流循环捕获 `finish_reason`；`length` → `output_truncated`、`content_filter` → `content_filtered`，均不判完成、不清 checkpoint | `tests/test_finish_reason.py`（4 例） |
| A2 在线 verify（第 1 层） | ✅ 完成 | run 收尾写 `verify.completion` 步骤：`evidence`（none/read_only/external/mutated）+ `tools_ok`/`mutating_ok`/`external_ok` + `finish_reason`/`end_reason`；`completed` 且零工具调用时打 `completion_without_evidence` 告警 | `tests/test_completion_evidence.py`（7 例）+ 真实 run 落库验证 |
| A2 在线 verify（第 2 层·透到 UI） | ✅ 完成 | `completionEvidence` 三处同源：终态事件（SSE camelCase）+ `chat_messages.completion_evidence` 落库 + 历史投影 `AssistantBlock.completionEvidence`；前端在耗时旁显示「已产生变更 / 外部工具 / 仅查询 / 未调用工具」 | `tests/test_completion_evidence_wiring.py`（6 例）+ 真实 run 双场景（`none` / `mutated`）SSE 与历史逐字一致 |
| B1 观测死角 | ✅ 完成（G6） | G1/G2/G3/G4 核查后确认已修；新增 `EDITH_LOG_FILE` 可选 RotatingFileHandler（自动建目录，dev 默认行为不变） | 落盘 + 默认 StreamHandler 双验证 |
| C1 约束存储 | ✅ 完成 | `constraints` 表（数据库层部分唯一索引去重）+ `ConstraintStore`（唯一权威源）+ 规则抽取 + 每轮重注入（预算 1200 字符，截断计数进 trace）+ 删会话级联 | `tests/test_constraints.py`（13 例）+ 真实 run：`constraints.inject {"injected": 2}` |
| C2 摘要硬丢失 | ✅ 完成 | `old_summary` 进摘要 prompt（此前只当失败兜底），摘要改为「基于已有摘要 + 新对话」 | `tests/test_summary_compress.py`（3 例） |
| C3 粗估 Token | ✅ 已存在 | CJK 感知估算已在（`_estimate_tokens` + 测试），无需施工 | `tests/test_stage10.py::test_estimate_tokens_cjk_aware` |
| A3 计划 + 证据绑定 | ✅ 完成 | `run_plans` 表（整表快照 + revision）+ `RunPlanRegistry`（把本 run 的 `tool_call_records` 引用交给工具）+ `plan_update` 工具 + `plan.updated` 事件 + 前端常驻面板。**done 必须带证据**：模型只声明「什么算做完」（`path` / `expect_exit_code`），服务端自己去本轮真实调用里找记录核对，核不过整批拒绝并列出候选调用 | `tests/test_run_plan.py`（22 例）+ `tests/test_plan_tool.py`（5 例）+ 真实 run：`rev1 pending → rev2 step1 done(verified)`、命令类证据误拒两次后被修正 |
| A2 离线评估集 / D 线 | ⬜ 待做 | — | — |

> 全量回归：**312 passed**；前端 `tsc --noEmit` 通过（`next build` 在本机挂起 >9 分钟未出结果，疑为外部字体/网络拉取，未作为验收依据）。

---

## 〇、为什么重排：一条主线把散点串起来

面试复盘反复出现一个关键词——**"从『我认为』进阶到『我如何证明』"**（第四节第 4 点，也是优先级最高的短板）。把它翻译成项目语言，就是同一件事：

> 系统目前**能跑、能落库、能展示**，但**不能证明"任务真的完成了"**。

这条主线串联起了散落在 V1 各处的条目：

| 面试短板 | 对应的项目真实缺陷 | V1 条目 | 本版位置 |
|---|---|---|---|
| 结果评估 / 验证是否真完成 | 假完成（无工具调用即完成） | P0-1 / P0-2 | **A1** |
| "我如何证明"（评估体系） | 无 Evals、trace 无 verify 步骤 | 未成条目 | **A2** |
| Agent 慢怎么定位（先分阶段计时） | LLM/准备/工具三处零打点、无归因模型 | G1–G6 | **B1** |
| 上下文压缩"哪些被压、如何降级" | 约束随摘要衰减、旧摘要不参与、粗估 token | P0-3 / context-eng | **C1/C2/C3** |
| 失败怎么办（重试条件/耗尽后恢复） | 并发无上限、结果体积无上限、checkpoint 不能当证据 | P1-1/2/4 | **D1/D2** |
| 协议/框架/业务边界 | Skill vs MCP、协议 vs 调度 | —（认知项） | 附注 |

**结论**：面试短板不是"知识缺口"，而是**项目里真实存在的工程缺口被暴露出来了**。修掉这些缺陷，面试答案自然就有证据、有排查路径、能追问。

---

## 一、A 线：结果验证（对应面试"结果评估 / 我如何证明"）

> 这是本版新增的**第一优先级**。V1 把"假完成"当 P0 bug，本版把它升级为"结果验证体系"的起点——面试里被明确追问"需不需要做结果评估，验证 Agent 是否真的完成"，答案是**必须做，且数据已经免费存在**。

### A1　假完成：完成判定必须由显式信号驱动，而非"没调工具"

**现状**：`execution.py:456` `end_reason` 初值即 `"completed"`；`execution.py:651` `if not tool_calls: return`；`service.py:318` 随之 `clear_checkpoints`。三个信号（UI / task_status / checkpoint）同时指向"完成"，trace 全绿、日志无 ERROR。

**面试关联**：第 1 题"只说代码真正支持的部分，避免追问无法举证"——而假完成恰恰是"无法举证"的典型：它伪装成成功，但事实不存在。

**建议（三层，从便宜到彻底）**：
1. **立即**：读 `finish_reason == "length"` → 不判完成，保持 `in_progress`（P0-2 的直接落地）。
2. **短期**：`completed` 由显式完成信号驱动（模型调用 `finish_task` 工具 / 结构化终止标记），无信号保持 `in_progress`。
3. **中期**：任务清单（见 A2）——完成 = 清单非空变空，且每项核销绑定成功的、路径匹配的工具调用记录。

**验证**：构造 `finish_reason=length` 响应，断言 `task_status` 仍 `in_progress` 且 checkpoint 未清。

### A2　Evals 落地：离线评估集 + 在线 verify（面试优先级最高的补强项）

**现状**：全项目无评测。48 个单测是"代码逻辑对不对"，不是"输出质量好不好"（`trace-eval-design.md` 已明确两者不可互替）。

**面试关联**：这是复盘"优先级 3：从我认为到我如何证明"的**唯一真写代码的落点**。面试里你已经能提出"先查检索再查上下文、加原文验证、模型评委防幻觉"，说明评估意识在，缺的是落地。

**建议（小而实，不做大而全）**：
1. **在线 verify（成本≈0，先做）**：每次 run 结束时，把"完成声明"与"事实集合"比对——事实已有：`tool_call_records`、B2 sync 的 `changed/deleted`、命令 exit code、`end_reason`。产出 = 消息上的 `unverified` 标记 + trace 一条 `verify.completion`（claims / verified / mismatch）。
2. **离线评估集（10–15 条）**：固定 machine-checkable 终态断言（文件存在 + 内容含标记 + `mvn compile` exit 0 等），每次改提示词 / 换模型跑一遍，统计"声称 vs 实际"偏差率。
3. **判定链原则**（面试已内化，落到代码）：判定者 = eval harness（后端脚本），不是模型；LLM-as-judge 只判语义质量，**永不判事实**。

**明确不做**：正则抓"已创建"、第二个 LLM 当裁判、`tool_calls_count > 0` 当唯一判据——三者都覆盖不到"调用了但失败 / 写错位置"。

### A3　任务计划 + 证据绑定（把"假完成"从文本升级为可核销状态）

**现状**：`backend-py` 无 plan / todo 概念（grep 无结果）；事件模型只有 delta / tool / 终态；前端无计划面板。

**面试关联**：DeepSeek harness 借鉴分析（10-24）已明确——计划若不绑证据，只是把"假完成"从一段文本升级成"一个打勾的列表，更可信但同样假"。

**建议**：
- `plan_update` 工具（mode=set/update，status 枚举）+ `run_plans` 表 + `plan.updated` 事件 + 前端常驻面板。
- **证据绑定是前提**：item 标 `done` 必须带 `tool_call_id` + `path`，服务端核对本 run 真实调用与 B2 `changed` 集合，否则拒绝更新。
- 三态区分：从未建清单（纯问答）/ 清单非空（未完成）/ 清单已清空（完成）——避免纯问答每次被续跑。

---

## 二、B 线：可观测与归因（对应面试"Agent 慢怎么排查 / 先分阶段计时"）

> 面试第 16 题明确要求"分阶段计时、链路追踪、首 Token 延迟、Token 数、重试记录确定瓶颈，不要只凭经验判断"。项目现状是"做了日志、没做结构"——有 request_id、有 tool_call_start/end，但六个死角未补。

### B1　补全六处观测死角（`trace-eval-design.md` §1.2 已列，未实施）

| 死角 | 后果 | 面试关联 |
|---|---|---|
| G1 异常零日志（`service.py:294-320` 无 `logger.exception`） | 页面显示"出错"，日志无字、堆栈永久丢失 | 第 16 题"定位根因"直接受阻 |
| G2 LLM 调用零打点（成功路径静默） | 慢/贵/答不符时无法定位哪一轮、输入多大、TTFT | 第 16/17 题核心 |
| G3 准备阶段黑盒（`configuration.py` 无日志） | "点了发送半天没反应"无法归因 | 同上 |
| G4 工具异常被吞（`except` 转字符串不打印） | 只看 `is_error=true`，不知为什么错 | 失败排查 |
| G5 业务错误无堆栈（`http_utils.py` 仅 warning） | 400/500 靠猜 | 同上 |
| G6 无跨 run 视角（日志仅 stdout，重启即失） | 无法统计错误率 / P95 / 模型分布 | 第 16 题"用数据定位" |

**建议**：补 `logger.exception` + LLM 调用打点（轮次/输入大小/TTFT）+ `RotatingFileHandler` 落盘。**没有这层，A2 的 Evals 和所有性能优化都只能靠猜**（面试第 16 题的原话）。

### B2　归因模型：把失败分为"模型 / 工具 / 环境 / 用户"四类

**现状**：有 fallback 显式标记（降级哲学），但无系统性归因——哪些失败是模型能力、哪些是环境过载、哪些是用户输入。

**面试关联**：第 16 题"不要只凭经验判断哪个环节最慢"；第 2 点"重试条件、哪些错误不该重试"。

**建议**：在 trace 里给每个失败打 `attribution` 标签，聚合出四类失败占比。这是从"能定位单次问题"到"能回答'系统整体健康吗'"的一步。

---

## 三、C 线：上下文工程（对应面试"上下文压缩 / System Prompt 是否参与压缩"）

> 面试第 2、3 题暴露：你能说出触发阈值、轮数、失败保留旧摘要，但**没答出"哪些被压、哪些保留原文、如何保留工具调用与结果的关联、摘要过长如何降级"**。这些正是 `context-engineering.md` 和 10-24 诊断里已识别但未修的缺口。

### C1　约束存储：把"不该被压缩的东西"隔离在压缩流水线之外（核心新增）

**现状**：约束（"必须用 Python""不要动 migrations/"）写在对话里，随递归再摘要逐代衰减；摘要 prompt 无约束 slot，指令性语句最容易被改写成陈述句而失效。

**面试关联**：第 2 题"哪些历史被压缩"、第 3 题"系统指令应独立管理，不当普通历史消息压缩"——你已答出方向，但项目未落地。

**建议（10-24 已出方案，未改代码）**：
- 新表 `constraints`（scope + text 原文不改写 + kind hard/soft + source 溯源 + check_spec 可机器校验）。
- 每轮重注入 system 固定段，永不进摘要；超预算按 priority 截断且**显式提示**，不静默丢弃。
- 压缩前先结构化抽取（constraints / decisions / open_items / artifacts），约束进表、摘要改结构化 slot。

### C2　修摘要硬丢失：旧摘要必须参与新摘要输入

**现状**：`execution.py:290` 注释说"旧摘要 + 全部历史"，但 `prompt_messages` 里 `old_summary` 没拼进去，只当兜底 → 早期信息**整体覆盖**（不是稀释，是硬丢）。

**面试关联**：第 2 题"哪些历史被压缩、哪些保留原文"——这是最容易被追问"那第 1 轮的决定呢？"的点。

**建议**：把 `old_summary` 拼进 prompt（"基于已有摘要 + 新对话输出更新摘要"）+ 补测试：摘要已存在 + 新历史不含早期信息，断言新摘要仍含早期实体。

### C3　修粗估 Token：`sum(len(content) // 4)` 对中文严重低估

**现状**：字符数 ÷4 粗估，中文 1 字 ≈ 0.6~1 token → 摘要触发**过晚**，中文长对话可能直接溢出。

**面试关联**：第 17 题"Token 数量"是定位瓶颈的四大指标之一，粗估会让这个指标失真。

**建议**：接入 tiktoken 或按语言分估（CJK 单独计）。

---

## 四、D 线：执行与稳定性（对应面试"失败怎么办 / 不能只看成功路径"）

> 面试第 2 点反复强调"重试条件、哪些错误不该重试、耗尽后如何恢复、如何被日志和测试验证"。项目已有有限重试 + 熔断，但三处边界未收口。

### D1　工具并发与结果体积无上限

**现状**：`asyncio.gather` 无信号量，模型一次发 20 个 read_file 会并发打向 E2B；结果体积只截断"较早"的，最近一条可能几十万字符直接进上下文。

**面试关联**：第 2 点"不能只看成功路径"——并发雪崩和上下文打穿都是"成功路径上没设卡"。

**建议**：
- 工具层加信号量（4–6）+ **按字节而非个数**限制（20 个小文件无害，3 个 10MB 是灾难）。
- 单条结果进 messages 前做体积上限 + 截断标记改为可执行指令（"需完整内容请调 read_file(path, offset)"），降低"说而不做"概率。

### D2　checkpoint 不能作为"做了什么"的证据

**现状**：`_digest` 只留 200 字符 + 哈希；`result_digest` 不区分成功失败；表无 `task_item_id`。

**面试关联**：A3 证据绑定的前置——想核销必须能回答"这次调用属于哪项、成功没有"。

**建议**：新增 `status`（success/failure）+ 可选 `task_item_id`；`args_digest` 保留关键字段（如 path）而非纯截断。

### D3　租约泄漏风险（workspace_lanes 实测发现的边界）

**现状**：`service.py:146` acquire 后、`223` create_task 前有 `configurations.load()`（读 DB）无兜底 try → 抛异常则 finally 不执行，该 workspace 永久 `workspace_busy` 直到重启。

**建议**：acquire 到 create_task 之间改 try/finally 释放（防锁死）。

---

## 五、E 线：部署与安全（V1 沿用，未变化）

> 与面试无关，但 V1 已列，保留不删。

- **E1** `secret.py` 是混淆非加密，云端多租户已上线需迁 KMS / 信封加密（V1 P1-6）。
- **E2** `deploy/nginx.conf` 缺 `client_max_body_size`；`Connection ""` 挂 WebSocket；`server_name _` 顺序依赖（V1 OPS-1/2/3）。
- **E3** E2B Volume 未开通（403 private beta），`local` 模式下 B1–B3 同步层仍是活跃路径（V1 EXT-1）。

---

## 六、建议施工顺序（按"面试短板暴露度 × 改动成本"排序）

| 顺序 | 条目 | 理由（对齐面试） |
|---|---|---|
| 1 | **A1 第 1 层**（读 finish_reason） | 改动最小，挡住最易检测的假完成，直接回应"结果评估" |
| 2 | **B1 观测死角**（G1/G2/G3） | 没有观测，A2 和所有性能优化都靠猜——面试第 16 题的根 |
| 3 | **C2 修摘要硬丢失** | 一处改动、信息不再硬丢，回应"哪些被压" |
| 4 | **C1 约束存储** | 止住"约束消失"，回应第 3 题"系统指令独立管理" |
| 5 | **A2 在线 verify + trace verify.completion** | 数据已有、成本≈0，回应"我如何证明" |
| 6 | **A3 计划 + 证据绑定** | 需要设计，单独立项，回应"结果评估"的完整闭环 |
| 7 | **D1 并发/体积上限** | 决定长任务能否跑完，回应"失败怎么办" |
| 8 | **A2 离线评估集** | 小而实，最后补，回应"可度量" |

---

## 七、面试认知项（不写代码，但影响表达）

以下不是代码缺陷，是面试表达时要注意的**职责边界**（面试第 3 点"区分协议、框架、业务逻辑"）：

| 易混点 | 正确边界 |
|---|---|
| Skill vs MCP | Skill 是可复用指令/工作流，不是工具执行器；MCP 是标准化接入协议，不替模型决定何时调工具 |
| 协议 vs 调度 | MCP 是协议 ≠ Agent 调度系统；Embedding 是语义表示 ≠ 表格结构恢复 |
| 完成信号 vs 事实 | "模型说完成" ≠ 事实完成；证据只能在系统侧采集，不能靠模型自述 |
| 降级 vs 重试 | 4xx 鉴权/上下文溢出不触发回退（配置问题非故障）；网络/5xx/超时才回退 |

---

## 八、结论

V1 是"代码 bug 清单"，本版是"**能力主线清单**"。核心变化：

1. **"结果验证"从 P0 bug 升级为第一优先主线**——面试明确追问"要不要做结果评估"，答案是必须做，且这条主线把假完成、Evals、trace verify 串成一条完整的"证明链"。
2. **观测死角从"可维护性"升为"根"**——面试第 16 题要求"先分阶段计时再定位"，而项目现状是三个关键环节零打点，这是所有"我如何证明"的物理前提。
3. **约束存储是新增的战略项**——它同时回应面试第 2/3 题，也是从"上下文压缩"到"上下文工程"的分水岭。

修完 A1 + B1 + C2（约 1–2 天），你在面试里答"上下文压缩""Agent 慢怎么排查""结果验证"这三类题时，就有了真实的证据、排查路径和验证指标——这正是复盘里"从我认为到如何证明"要补的那块。
