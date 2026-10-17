# ETHOS vs wuu 记忆系统对比与优化路线

> 状态：分析稿（未实施）
> 目的：以 wuu 记忆系统为参照，定位 ETHOS 记忆体系的优劣势，给出贴合「代码 Agent」定位的优化路线。
> 参照对象：wuu 的「记忆 = 普通 Markdown 文件 + Memory 插件」设计（索引注入、memdir、teaching、Fork 回流、Dream 后台整合）。

---

## 一、wuu 记忆系统核心设计（提炼）

1. **核心哲学**：记忆不是核心 loop 的数据模型，而是普通 Markdown 文件——核心只做一件事：把记忆索引注入上下文。
2. **四块记忆范围**：
   - **用户笔记本**（~/.wuu，跨项目）：4 种类型 `user` / `feedback`（带 why）/ `reference`（外部入口）/ `lesson`（经验教训）
   - **工作区记忆**（memory/MEMORY.md，单工作区跨会话）：架构决策、稳定约定、项目背景
   - **会话记忆**（单会话临时）：summary / checkpoint / notes——明确不保证跨会话
   - **命名 Agent 身份笔记本**（internal/memdir）：每个协作 Agent 一个目录，跨天跨任务的身份
3. **memdir 精妙设计**：
   - `MEMORY.md` 只是索引：`- [Title](topic.md) — one-line hook`，硬性保护 ≤200 行 / ≤25KB，超限截断+警告
   - 注入前逐行安全扫描（威胁模式 + 不可见 Unicode），违规行替换为 `[memory line removed: security]`
   - 每条记忆是独立 topic.md（frontmatter: name/description/type）
   - 写入走普通文件工具（memoryDir 加进 file tools 的 scope roots）——不需要专门记忆 RPC
   - teaching 文本教模型「两步保存法」，附「不存清单」：仓库可推导事实 / PR 号 / 提交 SHA / 原始日志一律不存
   - **Fork 回流**（merge.go）：命名 Agent 可 fork，退休时 MergeNotebook 合并回母体——字节相同跳过、同名冲突保留母体、fork 副本存 `-fork.md`，per-母体锁串行化
4. **后台整合与信任边界**：
   - Dream 插件后台定期扫描已完成会话，把稳定事实写回工作区记忆（默认关闭）
   - 信任边界：记忆会进模型上下文、可能影响工具选择；核心文件工具不把 WUU_HOME 当通用可写目录

---

## 二、ETHOS 现状（分层记忆）

| 层 | 名称 | 存储 | 关键点 |
|----|------|------|--------|
| L1 | 工作记忆 | 内存 | 单次请求 messages（execution.py 组装） |
| L2 | 短期记忆 | chat_messages 表 | 结构化（role/content/reasoning/image_ids）+ Timeline 投影 |
| L3 | 中期记忆 | session_summaries 表 | 40% 阈值自动折叠 + 防死锁 + 失败降级 |
| L4 | 长期记忆 | user_agents 表 | personality / default_model_id / timezone |
| L5 | 外部知识 | Skills（Volume/SKILL.md） | 摘要注入 + 正文按需读取 |

---

## 三、ETHOS 相对 wuu 的优势（4 个）

1. **结构化 DB 存储**：查询/投影能力远超纯文件——Timeline 投影、会话列表、按角色过滤、首条消息作标题都是 SQL；wuu 需自解析文件。
2. **会话压缩全自动**：40% 阈值 + 防死锁（溢出裁剪重试 3 次）+ 失败降级回退全量，运行时自动决策用户无感；wuu 的 compact 偏手动/用户触发。
3. **安全与凭据 fail-closed**：`secret.py` XOR 混淆落盘、SSRF 默认拒绝内网、沙箱路径白名单、MCP URL 校验——防御性编程显式留痕；wuu 主要靠文件系统 scope。
4. **可观测性**：request_id 贯穿 HTTP→gateway→agentrun→tool_call，日志带 duration/tokens/tools_called/三态——wuu 记忆系统本身不产生这些度量。

---

## 四、ETHOS 相对 wuu 的不足（6 个，按严重度）

| # | 不足 | wuu 做法 | ETHOS 现状 | 影响 |
|---|------|---------|-----------|------|
| 1 | **无项目级记忆**（最大 gap） | memory/MEMORY.md 存架构决策/稳定约定 | 每个会话从零开始 | 代码 Agent 场景最痛：换会话后项目上下文归零 |
| 2 | **无索引注入上限** | MEMORY.md ≤200 行/≤25KB 硬截断+警告 | Skills 摘要直拼 system，无硬性保护 | Skills 多了会撑爆上下文 |
| 3 | **无安全扫描** | 注入前扫描威胁模式+不可见 Unicode | 摘要/人格直接拼 prompt | 记忆被污染（注入/隐形字符）时无兜底 |
| 4 | **无 teaching 不存清单** | 明示不存 PR 号/SHA/原始日志 | 摘要 prompt 只说「不要重复密钥」 | 记忆混入可推导噪音数据 |
| 5 | **用户偏好维度太薄** | 4 类型（user/feedback/reference/lesson） | 3 字段（personality/模型/时区） | 缺「工作方式纠偏」「经验教训」等高价值记忆 |
| 6 | **无后台整合** | Dream 插件后台扫描已结束会话回写 | 只在会话内做摘要 | 跨会话经验无法自动沉淀 |

---

## 五、优化路线（按优先级）

> 核心判断：wuu 的精华不在存储形态（文件 vs DB），而在四个机制——**索引上限、安全扫描、teaching 引导、后台整合**。ETHOS 的 DB 优势是底层，这些机制是上层，两层正交，可全部借鉴而不冲突。

### P0 · 项目级记忆（补最大 gap，改动中等）
- 新增 `project_memories` 表（或复用 Volume 文件）：`(project_id, content, type, updated_at)`
- Agent 运行时按项目自动注入 system（等同 wuu 的 MEMORY.md）
- 价值：换会话不丢项目上下文（架构决策、稳定约定、项目背景）

### P1 · 索引上限 + 安全扫描（低改动，高安全收益）
- Skills 摘要注入前加硬上限（如 ≤200 行）+ 超限截断警告
- 注入前逐行扫描：不可见 Unicode（零宽字符）、威胁模式，违规行替换为 `[memory line removed]`
- 价值：防记忆污染，与 ETHOS 零信任哲学一脉相承

### P2 · teaching 不存清单 + 后台整合（中成本）
- 摘要 prompt 补「不存可推导事实（PR 号/SHA/日志）」指令
- 会话结束时让模型提炼「稳定事实」写入项目记忆（对齐 Dream 插件，但加用户确认，避免自动覆盖）

### 明确不做的
- ❌ Fork/MergeNotebook 协作机制——ETHOS 是单 Agent 多用户隔离，无「命名 Agent 副本退休回流」场景
- ❌ 记忆改文件存储——DB 是 ETHOS 优势，不需要向 wuu 看齐

---

## 六、面试话术（可直接背）

> 对比过 wuu 的记忆系统。它的核心设计是「记忆是 Markdown 文件 + 索引注入」：MEMORY.md 只是 ≤200 行的索引，每条记忆是独立 topic.md，注入前做安全扫描和硬截断，还有 teaching 教模型「什么不该存」。
> 我的项目是结构化 DB 方案，会话历史、摘要、用户画像分层存储，查询和投影能力强，会话压缩是全自动的——这是我们的优势。
> 借鉴 wuu 补齐三件事：① 项目级记忆（换会话不丢上下文）；② 索引注入上限 + 安全扫描（防记忆污染）；③ teaching 不存清单（记忆质量）。
> 不做 Fork 协作和文件化改造——场景不匹配，避免过度设计。
