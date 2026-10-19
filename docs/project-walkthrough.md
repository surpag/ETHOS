# ETHOS 全景导读

> 面向"第一次打开这个仓库"的读者。按一次请求的真实流动顺序组织，每个模块都回答三个问题：
> **它解决什么问题 → 为什么这样设计 → 不这样会怎样。**
>
> 代码事实截至 2026-10-18，后端 `backend-py/` 共 15,043 行 Python，19 个业务模块；前端 `web-v1/` Next.js App Router，44 个 BFF 路由。
> 文中所有 `文件:行号` 均已对照真实代码核对。

---

## 目录

- [1. 这个项目是什么](#1-这个项目是什么)
- [2. 代码地图](#2-代码地图)
- [3. 一个请求的完整旅程](#3-一个请求的完整旅程)
- [4. Agent 内核](#4-agent-内核)
- [5. 安全边界](#5-安全边界)
- [6. 状态、恢复与一致性](#6-状态恢复与一致性)
- [7. 工作区存储与同步](#7-工作区存储与同步)
- [8. 数据模型](#8-数据模型)
- [9. 用量与可观测性](#9-用量与可观测性)
- [10. 前端链路](#10-前端链路)
- [11. 部署与运维](#11-部署与运维)
- [12. 已知边界](#12-已知边界)
- [13. 设计原则复盘](#13-设计原则复盘)

---

## 1. 这个项目是什么

一句话：**一个多租户的云端代码 Agent**。用户在浏览器里对话，Agent 在远端沙箱里读写真实文件、执行命令、跑 Git，结果实时流回页面。

它不是"套壳 ChatGPT"，因为有四件事同时存在：

| 能力 | 意味着什么 |
|---|---|
| 真实文件系统 | 有一个 E2B 沙箱，Agent 的读写是真实持久化的，用户能看到文件树 |
| 工具循环 | 模型可以连续调用工具、观察结果、再决策，一轮对话可能跑几十次工具 |
| 崩溃可恢复 | 进程被 kill 掉后重新进来，Agent 知道"我上次做到第几步" |
| 多租户隔离 | 多个用户的文件、凭据、密钥、用量必须互不可见 |

前三件决定了这是个**有状态系统**，第四件决定了**所有状态都带 user_id 维度**。这两点是理解整个代码库的钥匙。

### 1.1 为什么是 Python

早期版本是 Go（`backend/`、`backend-v1/`、`backend-v2/` 仍在仓库里作为历史）。现在的主线是 `backend-py/`，定位是**基于 Python 从零开发的独立实现**，不是翻译移植：模块边界、依赖注入方式、错误分类都比 Go 版更收敛。

真正的原因是生态：Agent 相关的 SDK（OpenAI、E2B、MCP）都是 Python 优先，Go 版大量时间花在"补一个 Python 里现成的能力"上。

---

## 2. 代码地图

### 2.1 后端模块与规模

```
backend-py/src/edith_backend/
├── main.py            298 行   组合根：开库、建模块、连依赖、挂路由
├── config.py           56 行   环境变量加载
├── database.py         27 行   aiosqlite 连接（WAL）
└── internal/                  19 个业务模块，按规模排序
    ├── workspace/    2562 行   工作区、同步层、记忆、Volume 后端
    ├── agentrun/     1998 行   一次 run 的完整生命周期
    ├── sandbox/      1494 行   沙箱生命周期 + 工具实现
    ├── models/       1309 行   内置目录 + 自定义模型
    ├── userconfig/   1284 行   用户设置、凭据、MCP
    ├── trace/        1080 行   链路追踪 + 离线 CLI 查看器
    ├── gitops/        897 行   Git 命令白名单、bundle 导出、推送
    ├── session/       670 行   消息、摘要、检查点、状态
    ├── cronjob/       623 行   定时任务调度
    ├── images/        488 行   图片上传与 COS
    ├── conversation/  414 行   会话历史投影
    ├── usage/         317 行   Token 计量
    ├── volume/        224 行   E2B Volume 注册表
    ├── skills/        211 行   Skill 目录
    ├── gateway/       164 行   多渠道身份翻译
    ├── agentstream/   149 行   流式事件解码
    ├── webadapter/    134 行   Web 渠道适配
    ├── tools.py         ~     工具分类与错误包装（共享）
    ├── netguard.py      ~     SSRF 校验（共享）
    ├── systemtools/     63 行   时间等系统工具
    └── cronadapter/     50 行   定时渠道适配
```

### 2.2 依赖方向：funnel-layered

模块之间只允许**向下**依赖，且共享能力必须沉到底层，不能横着 import：

```
              main.py（唯一知道所有模块的地方）
                 │
     ┌───────────┼───────────┐
     ▼           ▼           ▼
  gateway    cronadapter  webadapter      ← 渠道层
     │           │           │
     └───────────┴───────────┘
                 ▼
             agentrun                     ← 编排层（不 import 任何具体渠道）
        ┌────────┼────────┐
        ▼        ▼        ▼
    sandbox   models   session            ← 能力层（互不依赖）
        │        │        │
        └────────┴────────┘
                 ▼
        tools.py / netguard.py           ← 共享基础（纯函数，无依赖）
                 ▼
            config / database
```

**为什么这两个"杂项文件"要放在 `internal/` 顶层而不是某个模块里？** `netguard.py` 的注释写得很清楚：

> 放在 `internal/` 顶层单文件而不是某个业务模块里，是因为它被 gitops 与 models 共用；若长在 gitops 下，models 导入时会被迫连带加载 e2b（gitops 包 `__init__` 依赖 executor），把一个纯字符串校验变成一条重依赖。

这是一个很实际的判断：**共用工具的位置决定了依赖图的形状**。放错地方，纯函数会拖来一整个 SDK。

### 2.3 装配方式：显式依赖注入

`main.py` 是整个进程唯一知道所有模块的地方。没有全局单例、没有服务定位器，所有依赖在 `lifespan()` 里显式 `new` 出来再互相传递：

```python
# main.py:119-140
case db = await open_database(config.database_path())
await create_session_schema(db)
await create_volume_schema(db)
await create_workspace_schema(db)
...

users = await new_userconfig_module(db, DEFAULT_MODEL_ID)
custom_models = CustomModels(db, users.providers)
model_catalog = new_models_module(users.providers, custom_models)
usage = await new_usage_module(db)
session_store = SessionStore(db)
conversations = new_conversation_module(session_store, usage.reader)
```

好处很直接：**测试时能精确替换任何一个依赖**，而且启动顺序的依赖关系在代码里是显式的（schema 必须先建完才能用 store）。

代价是 `main.py` 会变长——这是有意的取舍，组合根就该是唯一"知道太多"的地方。

---

## 3. 一个请求的完整旅程

用户在输入框敲一句"帮我把 README 里的端口改成 9000"，按下回车。以下是接下来 500 毫秒到 3 分钟内发生的事。

### 3.1 全景

| 段 | 位置 | 职责 | 关键设计 |
|---|---|---|---|
| L0 | 浏览器 | 乐观插入用户消息、生成 requestId | requestId 由前端生成，全程复用 |
| L1 | Next.js BFF | 补上服务端解出的 userId | **userId 绝不由浏览器提供** |
| L2 | `gateway/service.py` | 渠道身份 → Clerk userId | 不可信渠道强制重写 sessionId |
| L3 | `agentrun/service.py:111` | 四道准入 + 开 trace | 拿锁顺序 A→B，释放必须 B→A |
| L4 | `agentrun/configuration.py:63` | 7 个子步骤聚合配置 | 每步单独打点，准备阶段不再是黑盒 |
| L5 | `agentrun/execution.py:427` | 流式模型调用 + 工具循环 | 上下文治理、熔断、checkpoint |
| L6 | `asyncio.Queue` → SSE | 事件边产生边推送 | 事件与持久化解耦 |

下面逐段展开。

### 3.2 L0 前端：乐观更新与 requestId

`chat-page.tsx:509` 的 `sendMessage` 做四件事，顺序有讲究：

```typescript
const requestID = crypto.randomUUID();
activeRequestIDRef.current = requestID;
setActiveRequestID(requestID);
savePendingRun({ requestId: requestID, sessionId: sessionID });   // ← 先存盘
const userBlock: UserBlock = { ... };
setSessions(...)                                                   // ← 再乐观插入
```

**先写 sessionStorage 再改 UI。** 顺序反过来的话，如果用户在这一瞬间刷新页面，pending 记录就丢了——后台任务还在跑，但页面再也不会去认领它。

`requestId` 由前端生成而不是后端，是为了让"乐观插入的消息"和"最终返回的结果"能对上号。后端全程使用这个 ID：日志、trace、usage 主键、SSE 事件里都是它。

### 3.3 L1 BFF：userId 的信任边界

`app/api/chat/stream/route.ts` 只有 30 行核心逻辑，但有一条不能破的规则：

```typescript
export async function POST(request: Request) {
  const { userId } = await auth();          // ← 服务端从 Clerk session 解出
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const chatRequest = await parseChatRequest(request);
  const agentRequest: GatewayMessageRequest = { ...chatRequest, userId };  // ← 覆盖
  ...
}
```

注释写得很直白：

> It intentionally derives userId from Clerk instead of accepting it from JSON.

`parseChatRequest` 是一个**严格的白名单校验器**：字段缺一个就返回 null，`requestId` 必须是标准 UUID，`sessionId`/`modelId` 不能是空串，`message` 为空时必须有图片或附件。这不是过度防御——后端 `/internal/*` 是内网接口，不应该再承担一遍参数校验的开销和错误分支。

这里还留着一个踩过的坑的注释：

```typescript
// 本次修改：透传 workspaceId（此前 BFF 白名单漏掉该字段，导致后端
// workspace_id 恒为空，工具层无法自动补全工作区路径、system prompt 也不注入工作区上下文）
```

**BFF 白名单式透传的代价**：任何新增字段都要在两处同步登记，漏一处就是静默降级（不报错，只是功能悄悄没了）。这是这类架构的固有成本。

同时 BFF 会透传 `X-Request-ID` 给后端，让前后端日志能用同一个 ID 串起来。

### 3.4 L2 gateway：多渠道的统一入口

`gateway/service.py` 是所有 Agent 渠道的唯一入口。今天有 Web 和定时任务，将来会有 IM。

它做的事情只有一件：**把外部身份翻译成 EDITH 内部身份**。

```python
def _is_trusted_channel(channel: str) -> bool:
    return channel in (WEB_CHANNEL, CRON_CHANNEL)

...
user_id = input_msg.external_user_id
session_id = input_msg.session_key
if not _is_trusted_channel(input_msg.channel):
    bound_user_id, found = await self.bindings.to_clerk_user_id(
        input_msg.channel, input_msg.external_user_id
    )
    if not found:
        return None, Error("identity_not_bound", "channel user is not bound to an EDITH user")
    user_id = bound_user_id or ""
    session_id = f"{input_msg.channel}:{user_id}"     # ← 强制重写
```

两处设计值得记住：

**① 可信渠道 vs 不可信渠道。** Web 和 cron 的 `external_user_id` 本来就是 Clerk userId（因为 BFF 已经从 session 里解出来了），直接用。而未来的 IM 渠道传来的是"微信 openid"之类的外部 ID，必须查绑定表。查不到就拒绝，**不自动注册**——自动注册等于任何人只要发条消息就能拿到一个账号。

**② 不可信渠道的 session_id 被强制重写为 `{channel}:{user_id}`。** 如果不重写，外部就能指定任意 session_id 去撞别人的会话。重写之后，一个渠道用户只有一个固定会话，攻击面归零。

### 3.5 L3 准入：四道门

`agentrun/service.py:111` 的 `run()` 是全项目最关键的 40 行。它的目标是：**用最便宜的代价拒绝掉不该跑的请求**。

```python
async def run(self, request: Request) -> tuple[Stream | None, Error | None]:
    request = _normalize_request(request)
    if err := _validate_request(request):
        return None, err

    tracer = new_tracer(self.traces, request_id=..., ...)
    await tracer.begin()

    async def _abort(error: Error):
        await tracer.finish(status=ERROR, end_reason=error.type, ...)
        return None, error

    if not self.lanes.acquire(request.user_id, request.session_id, request.request_id):
        return await _abort(Error("session_busy", ...))

    if request.workspace_id and not self.workspace_lanes.acquire(...):
        self.lanes.release(request.user_id, request.session_id, request.request_id)  # ← 关键
        return await _abort(Error("workspace_busy", ...))

    async with tracer.step(PREPARE, "load_configuration") as s:
        configured = await self.configurations.load(request, tracer)
    ...
```

四道门各自的理由：

| 门 | 挡的是什么 | 不加会怎样 |
|---|---|---|
| `_validate_request` | 参数缺失 | 走到模型调用才发现，浪费一次连接 |
| `lanes.acquire` | 同一会话并发两个 run | 两个 run 同时往 chat_messages 写，assistant 消息归属混乱 |
| `workspace_lanes.acquire` | 同一工作区两个会话 | 两个沙箱互相覆盖文件（见 §7） |
| `configurations.load` | 模型不存在 / 没配密钥 | 用户等 30 秒才看到"模型不可用" |

**两个容易漏的细节：**

前一个是锁的顺序。获取顺序是 `lanes → workspace_lanes`，所以放弃工作区锁时**必须先还回会话锁**：

```python
if request.workspace_id and not self.workspace_lanes.acquire(...):
    self.lanes.release(...)      # 漏了这行 = 该会话永久繁忙，只能重启进程
    return await _abort(...)
```

后一个是 trace 的收尾。`tracer.begin()` 在最前面，所有早退分支都走 `_abort()` 把 trace 收成失败态。否则你会看到一堆"运行中"的僵尸记录，永远分不清是真在跑还是早就死了。

**准入通过之后，run 是异步启动的：**

```python
queue: asyncio.Queue[Event | None] = asyncio.Queue()
task = asyncio.create_task(self._run(configured, run, queue, tracer))
self.active[request.request_id] = ActiveRun(user_id=..., session_id=..., task=task)
return Stream(events=queue), None
```

`run()` 立刻返回一个 `Stream`（本质是个队列），真正的执行在后台 task 里。这是 SSE 能做到"边跑边推"的结构基础——如果 `run()` 同步等待整个 Agent 跑完，前端就只能拿到最终结果。

`self.active` 字典同时承担了 `/status` 查询和 `/cancel` 取消的索引职责。

### 3.6 L4 准备：把黑盒拆成 7 步

`configuration.load()` 的注释直接说明了动机：

```python
# 每个阶段一个 prepare 子步骤：准备阶段曾经是整体黑盒，
# 「点了发送半天没反应」时无法区分是这一步慢还是模型慢。
```

七个步骤，每步一个 `tracer.step`：

| 步骤 | 做什么 | 失败返回 |
|---|---|---|
| `model.resolve` | 解析模型 ID → 拿到 `ModelDefinition` | `unsupported modelId` |
| `provider.api_key` | 取该供应商的密钥（解密） | `load model credential` |
| `settings.personality` | 读用户人格提示词 | 不失败（空则跳过） |
| `uploads.validate` | 校验上传文件确实在 uploads/ 下 | `prepare uploaded files` |
| `skills.summaries` | 读内置 + 用户 Skill 摘要 | 不失败 |
| `workspace.memory` | 读工作区指令 + 自动记忆 | 不失败 |
| `images.urls` | 图片 ID → 短期签名 URL | `load image input` |

**模型解析的优先级与回退规则**（`configuration.py:67-86`）是这一节最值得学的设计：

```python
explicit = bool(request.model_id)          # 用户是否明确指定
if not request.model_id:
    request.model_id = await self.settings.load_default_model_id(request.user_id)
if not request.model_id:
    request.model_id = DEFAULT_MODEL_ID

definition = await self.models.find_for(request.user_id, request.model_id)
if definition is None and not explicit and request.model_id != DEFAULT_MODEL_ID:
    request.model_id = DEFAULT_MODEL_ID    # 只有"默认值失效"才偷偷换
    definition = await self.models.find_for(request.user_id, request.model_id)
if definition is None:
    return Error("invalid_request", "unsupported modelId")   # 显式指定失效就报错
```

区分 `explicit` 的理由：用户默认值和平台默认值本质是「**可能失效的引用**」——用户删掉自己加的自定义模型后，默认值会指向一个不存在的 ID。这时悄悄回退到平台默认是体贴。

但如果用户在界面上明确选了模型 A，后端偷偷换成 B，那是在骗他。此时报错才是诚实的。

**system prompt 的分层拼装**（`configuration.py:137-170`）：

```python
system_instruction = _BASE_IDENTITY + personality
if workspace_memory:
    system_instruction += "\n\n【项目背景】\n" + workspace_memory
instruction = run_instruction(system_summaries, user_overview, uploads)
if instruction:
    system_instruction += "\n\n" + instruction
if request.workspace_id:
    system_instruction += "\n\n【当前会话工作区】\n..."
```

一共五层：基础身份 → 用户人格 → 工作区记忆 → Skill 摘要 + 上传文件 → 工作区路径上下文。

这里有一处修过的真实 bug，注释保留在 `configuration.py:155`：

```python
# 本次修改：删除下方对 system_instruction 的重复整体赋值（曾覆盖掉第 6.5 步注入的
# 【项目背景】/workspace_memory，属既有 bug），改为只追加 instruction 与工作区上下文。
```

**中间任何一次用 `=` 而不是 `+=`，前面拼的全丢。** 这类 bug 不报错，只是模型"突然不记得项目背景了"，极难排查。

最后那段工作区上下文也值得看，它解决的是"模型不知道自己在哪"的问题：

```
【当前会话工作区】
当前会话关联工作区 ID：{workspace_id}。
你的文件操作基于该工作区（Sandbox 内路径 workspace/{workspace_id}/...）。
调用 Sandbox 文件工具时无需关心路径前缀，直接传相对路径（如 README.md、src/main.py）即可，
工具会自动定位到该工作区。
```

Web 场景的模型没有"当前目录"这个概念（不像 CLI Agent），不显式告诉它，它会传绝对路径或猜前缀。这段提示词配合工具层的路径自动补全（§5.2），把"路径正确性"这件事从模型的责任转移到了系统的责任。

### 3.7 L5 主循环

见 §4。这是整个项目最核心的 400 行。

### 3.8 L6 回流：事件与持久化解耦

`_run()` 的骨架（`service.py:258`）：

```python
try:
    queue.put_nowait(Event(type="run.started", ...))
    await self.store.set_task_status(user_id, session_id, "in_progress")
    ... 打开 MCP ...
    ... execution.chat(..., queue, ...) 往队列里塞事件 ...
except asyncio.CancelledError:
    reason = "canceled"
    ...
finally:
    queue.put_nowait(Event(type=<终态事件>, assistantId=..., durationMs=...))
    queue.put_nowait(None)          # 哨兵：告诉 SSE 生成器流结束
    ... 释放锁、清理 MCP、写 usage、收尾 trace ...
```

三个设计点：

**① `None` 作为流结束哨兵。** `Queue[Event | None]` 中 `None` 表示"没有更多事件了"。这比用异常或超时判断结束要干净得多。

**② 终态事件带 `assistantId` 和 `durationMs`。** 因为前端需要把耗时挂到正确的助手块上，而且失败也要给耗时（`run.error` 同样带）。

**③ SSE 生成器在 `finally` 里一定会收到终态事件。** 无论正常完成、轮次耗尽、异常还是取消，客户端都不会永远挂着等。

---

## 4. Agent 内核

`execution.py` 891 行，其中 `chat()` 从 427 行到 834 行。这一节按"一轮内发生什么"讲。

### 4.1 循环骨架

```python
for _round in range(MAX_TOOL_ROUNDS):
    _trim_tool_results(messages)                       # C1 上下文治理
    budget = int(configured.definition.context_window * TOOL_CONTEXT_BUDGET_RATIO)
    if _estimate_tokens(messages) > budget:            # C2 token 预算
        end_reason = "context_budget_exhausted"
        break

    content_parts, reasoning_parts = [], []
    tool_calls: dict[int, dict[str, str]] = {}
    usage: CompletionUsage | None = None

    async with tracer.step(LLM_CALL, f"create#{_round+1}", ...) as llm_step:
        ... 流式消费 ...

    if not tool_calls:                                 # 模型主动收尾
        await store.add_assistant_message(...)
        return usage_acc.tokens(), used_model_id, tool_calls_count, end_reason, duration

    ... 执行工具、后处理、回填 messages ...
```

四种出口：`completed`（模型不再调工具）、`rounds_exhausted`（轮次用完）、`context_budget_exhausted`（预算耗尽）、异常/取消。

### 4.2 流式解码：一个必须写对的顺序

```python
async for chunk in stream:
    # usage 只出现在流末尾那个「choices 为空」的收尾块里
    # （stream_options={"include_usage": True} 的协议约定）。
    # 必须在跳过空 choices 之前捕获，否则真实用量永远拿不到，
    # 只能退化成本地估算（估算不含缓存字段 → 命中率恒为未知）。
    if chunk.usage is not None:
        usage = chunk.usage
    if not chunk.choices:
        continue
```

**这两行的顺序错了，成本统计就永久失真，而且不报错。**

OpenAI 流式协议里，真实用量只在最后一个 `choices == []` 的块里。如果把 `if not chunk.choices: continue` 写在前面，那个带 usage 的块会在捕获之前被跳过，代码静默走 `_estimate_tokens` 本地估算分支。表现就是"token 只能算输出"、"缓存命中率永远是未知"。

### 4.3 工具调用的三次归并

模型返回的工具调用是**分片**的，且多个工具的参数碎片会交错。第一片只有 `id` 和 `name`，后续几十片只有 `arguments` 的片段：

```python
if getattr(delta, "tool_calls", None):
    for tc in delta.tool_calls:
        slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
        if tc.id:                    slot["id"] = tc.id
        if tc.function and tc.function.name:
                                     slot["name"] = tc.function.name
        if tc.function and tc.function.arguments:
                                     slot["arguments"] += tc.function.arguments
```

关键是 `setdefault(tc.index, ...)`：**按 index 归并，不按到达顺序 append**。如果按 append 收集，拿到的是拼接错乱的 JSON，`json.loads` 直接失败，工具永远执行不了。

归并之后还有两次排序处理：

```python
ordered = [tool_calls[i] for i in sorted(tool_calls)]   # 按 index 排序
```

以及后处理阶段的严格顺序（见 4.5）。

### 4.4 并发策略：一票否决

```python
# 只读工具可并发；含写工具则整批串行（一票否决，对齐 Pi 调度策略）
if len(ordered) > 1 and all(_is_readonly_tool(tc["name"]) for tc in ordered):
    executed = await asyncio.gather(*[_run_one(tc) for tc in ordered])
else:
    executed = [await _run_one(tc) for tc in ordered]
```

判据是 `all(...)`，不是"大部分是只读"。因为写操作之间有依赖：`写 a.py` 和 `读 a.py` 同时发，结果取决于调度顺序。

宁可慢一点，也不能让结果依赖调度。这是**确定性优先于吞吐**的取舍。

### 4.5 后处理：为什么"执行可以并发，后处理必须串行"

```python
# 后处理严格按调用顺序（保证 checkpoint / 熔断 / 事件 / 消息顺序确定）
for tc, (result, duration_ms) in zip(ordered, executed):
    tool_calls_count += 1
    if not _is_readonly_tool(tool_name):
        read_cache.clear()

    # WAL：本次工具调用完成后立即写进度日志（先记录，再做熔断判断，
    # 确保即使随后熔断/中断，本次进度也已落盘；独立于 run 终止）
    await store.append_checkpoint(...)

    retryable = _parse_result_retryable(result)
    if retryable is False:
        consecutive_failures[tool_name] = consecutive_failures.get(tool_name, 0) + 1
        if consecutive_failures[tool_name] >= TOOL_FAILURE_CIRCUIT_BREAKER:
            raise ToolCircuitBreakerError(tool_name, ...)
    else:
        consecutive_failures[tool_name] = 0

    queue.put_nowait(decoder.tool_finished(tc["id"], status, result))
    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
```

四个细节，每个都有理由：

**① checkpoint 写在熔断判断之前。** 注释说明了：先记录再判断，确保即使随后熔断，本次进度也已落盘。如果顺序反过来，"熔断"这条路径本身会丢掉最后一步的进度——而熔断恰恰是最需要知道"我做到哪了"的场景。这和数据库先写 log 再改数据是同一个思路。

**② 熔断按工具名分别计数。** 文件工具连续坏了，不该拖累命令工具。`consecutive_failures: dict[str, int]`。

**③ 只统计 `retryable=False`（系统故障）。** "文件不存在"是业务错误，模型该换个路径重试，不该熔断。如果把业务错误也算进去，正常的试错过程会触发熔断，Agent 被自己误杀。

**④ 只读缓存遇到写操作全清。** 一次写可能影响任何文件，局部失效不可靠。

### 4.6 上下文治理三层

长会话会撞上下文窗口上限。项目用了三层递进的策略：

| 层 | 位置 | 做法 | 触发条件 |
|---|---|---|---|
| C0 摘要压缩 | `summary_compress()` `:243` | 把久远历史折成一段摘要 | 消息量超过 `HISTORY_THRESHOLD_RATIO=0.4` |
| C1 结果截断 | `_trim_tool_results()` | 只保留最近若干条工具结果全文，更早的压成摘要 | 每轮开始 |
| C2 预算收尾 | `chat()` `:512` | 估算 token 超 `context_window × TOOL_CONTEXT_BUDGET_RATIO` 就主动收尾 | 每轮开始 |

C2 的意义是**主动收尾而不是硬撞**。如果不管，模型 API 会返回 `context_length_exceeded`，那一轮的内容全丢。主动收尾至少能保住已有产出，并把 `end_reason` 标记成 `context_budget_exhausted`，前端据此提示"上下文用完，请开新会话"。

摘要压缩本身是一次模型调用，所以它的用量必须计入本次 run（`chat()` 里 `usage_acc.add(summary_tokens)`）。这是很容易漏的一笔账——摘要 prompt 通常很大（把整段历史塞进去）。

### 4.7 模型回退链

```python
async def _first_model_call(configured, fallback_models, messages, all_tools, attempts):
    """第一轮模型调用：沿「当前模型 → 回退候选」尝试。

    openai SDK 的 max_retries=2 已对网络/5xx/超时等可重试错误做指数退避重试；
    候选之间是模型级回退，失败原因只打 warning 不中断，全部失败才抛出最后一个错误。
    """
    candidates = [(configured.definition, configured.api_key)]
    if fallback_models:
        candidates.extend(fallback_models)
    for definition, api_key in candidates:
        try:
            client = AsyncOpenAI(api_key=api_key, base_url=definition.base_url, max_retries=2)
            stream = await client.chat.completions.create(...)
            return client, definition, stream
        except Exception as exc:
            attempts.append({"model": definition.id, "error": ..., "duration_ms": ...})
            logger.warning("model call failed model=%s error=%s, trying next", ...)
    raise last_error
```

**两层重试的分工很清晰**：SDK 的 `max_retries=2` 处理瞬时故障（网络抖动、5xx、超时），模型级回退处理"这个模型彻底不可用"（配额耗尽、模型下线）。

`attempts` 列表被就地追加后挂到 trace step 的 metadata 上，所以"模型全链失败"在 trace 里能逐条定位每次尝试的模型、错误、耗时。注释说明了这个改进的动机：

> 此前只有一行 warning。

**只有第一轮走回退链。** 后续工具轮次复用已经确定的模型——否则第二轮又回退到别的模型，对话上下文的一致性会崩。

### 4.8 收尾：为什么必须保留最后一轮

```python
# 循环结束：预算耗尽已在循环内设 end_reason；否则视为轮次耗尽
if end_reason == "completed":
    end_reason = "rounds_exhausted"

# 保留最后一轮的推理与文本：此前 reasoning 硬编码为空，导致刷新后历史里丢失全部推理
await store.add_assistant_message(
    user_id, session_id,
    "".join(content_parts),
    "".join(reasoning_parts),      # ← 曾经是 ""
    workspace_id, tool_call_records, duration,
)
```

这个 bug 很典型：轮次耗尽时，前面每轮的文本都通过 `tool_calls` 进入了 messages，但**最后一轮的推理没有对应的工具调用**，如果不显式保存就永久丢失。用户刷新页面后发现"推理过程没了"。

---

## 5. 安全边界

这一章是理解"Agent 能做什么、不能做什么"的关键。核心原则一句话：**能力边界要靠结构保证，不能靠提示词请求。**

### 5.1 信任边界总图

```
浏览器（不可信）
   │  ① userId 由服务端注入，浏览器无法伪造
   ▼
Next.js BFF（可信，有 Clerk secret）
   │  ② /internal/* 是内网接口
   ▼
后端 gateway（可信）
   │
   ├─► 模型 API（外部，用户可填 base_url → 必须过 SSRF 校验）
   ├─► E2B 沙箱（外部，出口白名单限制）
   │      └─► Agent 工具执行（模型可控的参数 → 必须过路径授权）
   └─► GitHub（外部，凭据只在后端内存里）
```

四类外部交互，四套不同的防护。

### 5.2 路径授权：唯一的卡点

Agent 的文件操作路径**完全由模型生成**，模型可能被提示注入诱导去读别的用户的工作区。防线是 `workspace_path()`（`sandbox/workspace.py`）：

```python
def workspace_path(input_path: str, allow_root: bool, current_workspace_id: str = "") -> str:
    """把工作区相对路径转为 Sandbox 绝对路径，并拒绝越权路径。

    安全策略：
    - 路径必须相对 WORKSPACE.root
    - 禁止绝对路径（以 '/' 开头）
    - 禁止 '..' 跳出
    - 必须位于白名单子目录下：uploads/、work/、artifacts/、workspace/、skills/
    - workspace/ 下的路径必须带有 workspace_id（不能直接访问 workspace/ 根）
    - **授权校验**：current_workspace_id 非空时，workspace/ 下的第二段必须等于它
    """
```

七条规则里，最后一条是真正的隔离：

```python
if top_dir == "workspace":
    target_workspace_id = parts[1]
    if current_workspace_id and target_workspace_id != current_workspace_id:
        raise PermissionError(...)     # 一个会话只能访问自己的工作区
```

**唯一卡点的实现方式**：所有文件类工具的所有路径字段都登记在 `_PATH_FIELDS` 里，校验函数遍历这个表：

```python
_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "sandbox_list_files": ("path",),
    "sandbox_read_file": ("path",),
    "sandbox_write_file": ("path",),
    "sandbox_make_directory": ("path",),
    "sandbox_delete_path": ("path",),
    "sandbox_move_path": ("from", "to"),      # ← 两个字段都要校验
}

def _scope_violation(name: str, args: dict, workspace_id: str) -> str:
    """这是唯一的授权卡点：所有文件类工具、所有路径字段都经此校验，
    新增工具只要登记到 _PATH_FIELDS 就自动受约束。"""
    for key in _PATH_FIELDS.get(name, ()):
        ...
```

注意 `sandbox_move_path` 有 `("from", "to")` 两个字段——**只校验一个就等于没校验**（可以从自己的目录 move 到别人的目录）。

登记表还有测试兜底：

```python
# 新增文件类工具时必须同步登记（tests/test_stage10.py 有断言，
# 漏登记会测试失败而不是静默放行）
```

这一句注释是整章最重要的设计思想：**让"漏做"变成测试失败，而不是静默放行**。

### 5.3 敏感路径：Agent 不该碰的东西

```python
_SENSITIVE_SEGMENTS = {
    ".git", ".hg", ".svn",                                    # 版本库元数据
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",             # SSH 私钥（.pub 公钥放行）
    "credentials", "credentials.json", "auth.json",           # 凭据文件
    ".netrc", "netrc",
}
```

外加 `.env` 和 `.env.*` 的特殊判断（`low == ".env" or low.startswith(".env.")`）。

**为什么连 `.git` 都禁？** 注释说了：`.git` 是 git 能力的根基，删了 git 工具就废。Agent 想回滚可以直接用 `git checkout`，但**不应该直接编辑 `.git` 内部文件**——那会绕过 git 的一致性保证，产生一个损坏的仓库。

**为什么 `.env` 单独判断？** 因为它不在 `_SENSITIVE_SEGMENTS` 集合里用精确匹配就能覆盖——`.env.local`、`.env.production` 都要拦。前缀匹配才能覆盖这些变体。

### 5.4 出口白名单：沙箱能连哪里

沙箱里的 `pip install`、`curl`、`npm install` 都需要出网。完全放开等于给了 Agent 一个任意 C2 通道。

```python
# E2B 硬限制：每个沙箱最多放行 10 个域名，超出服务端直接 400 且不可提额
MAX_ALLOWED_DOMAINS = 10

async def _resolve_allowed_domains(self, user_id, workspace_id) -> list[str]:
    """解析出口白名单：workspace metadata.allowedDomains 优先，否则取环境变量默认。
    返回空列表 = 不限制（E2B 默认放通全部出口）。"""
```

下发策略：

```python
await sandbox.update_network({"allow_out": list(domains), "deny_out": [ALL_TRAFFIC]})
```

两个取舍：

- **`update_network` 是整体替换**（省略字段会被清空），所以只在白名单非空时下发，传空对象即恢复全放通。不主动恢复，避免每次连接多一次往返。
- **下发失败不阻塞连接，但必须显式告警**——「不能静默当作已收紧」。安全配置的失败必须可见，否则你以为收紧了，其实全开着。

`_net_policies` 字典按 `sandbox_id` 缓存已下发的策略，避免每次连接重复 `update_network`。

### 5.5 SSRF：为什么单独拆一个函数

`netguard.py` 被 gitops（clone URL）和 models（用户填的 base_url）共用。核心是两层判断：

```python
def assert_public_host(host: str) -> None:
    """host 必须是公网主机名或 IP；非法时抛 HostRejected。"""
    host = (host or "").rstrip(".").lower()
    if not host:
        raise HostRejected("missing host")
    if host == "localhost" or host.endswith(".localhost"):
        raise HostRejected("localhost is not allowed")
    reject_metadata_host(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 非 IP 字面量 → 视为域名，放行
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        raise HostRejected("private/loopback addresses are not allowed")
```

**为什么 `reject_metadata_host` 要拆成独立函数？** 注释回答了：

> 独立成一个函数，是因为「允许本地端点」的开关会放宽回环/私网判断，但**不能**顺带放开元数据 IP —— 否则本地部署就会变成读云凭据的跳板。

云元数据端点（`169.254.169.254` 等）的杀伤力比普通内网大得多：一次 SSRF 就能拿到实例的临时凭据，进而接管整个云账号。所以它必须独立于任何"放宽"开关，**无条件拒绝**。

`assert_public_host` 的已知边界也写在注释里：

> 非 IP 字面量 → 视为域名，放行（域名解析后的私网 IP 由部署层网络策略兜底）

这是有意的取舍：DNS 解析发生在 SDK 内部，应用层拿不到最终 IP。完全的 SSRF 防护需要 DNS pinning 或网络层拦截，那超出应用层职责。**承认边界的范围，比假装全覆盖更可靠。**

### 5.6 Git：三层白名单

`gitops/policy.py` 的文件头注释给出了整体设计：

> 三层校验：
> - 第一层 全局拦截：`--config-env` / `--exec-path`（可让 git 加载任意可执行文件或环境变量，是提权入口）、shell 元字符
> - 第二层 子命令级：只读 + clone + 本地写（add/commit）默认放行；远端写（push/pull/fetch）仅当 `allow_remote=True`
> - 第三层 参数级：add 只收显式路径；commit 必须带消息且禁审计敏感 flag；远端禁 force 等
>
> **默认拒绝意味着：漏登记 = 越权，而不是漏拦 = 越权。**

这是整章最重要的一句话。白名单 vs 黑名单的本质区别不是"哪个更全"，而是**失败时倒向哪边**：

| | 新增一个子命令 | 结果 |
|---|---|---|
| 黑名单 | 忘了加进禁止列表 | **默认放行**，越权 |
| 白名单 | 忘了加进允许列表 | **默认拒绝**，功能不可用但不危险 |

功能不可用会被用户报上来，越权不会。

几个具体规则及其理由：

```python
# 危险 commit flag（审计生命线）：--no-verify 会跳过 hook（= 绕过平台服务端校验）；
# --author/--date 可伪造归属；-c/-C 可复用任意 commit 的消息/作者；--gpg-sign 等签名相关一律禁。
BLOCKED_COMMIT_FLAGS = frozenset({
    "--no-verify", "--gpg-sign", "--no-gpg-sign", "-S",
    "--signoff", "--fixup", "--squash", "-C", "-c", "--author", "--date",
})

# `add` 只接受显式路径：拒绝 `.` / `-A` / 通配符，避免误暂存敏感或无关文件。
_BLOCKED_ADD_ARGS = frozenset({".", "-A", "--all", "-u", "--update", ...})

# 远端子命令禁用的危险 flag：强推、删远端分支、镜像、跳过 hook。
_BLOCKED_REMOTE_FLAGS = frozenset({
    "--force", "-f", "--force-with-lease", "--force-if-includes",
    "--mirror", "--delete", "-d", "--prune", "--tags", "--all", "--no-verify",
})
```

`add` 拒绝 `.` 的理由值得单独说：`git add .` 会把工作区里所有文件加进索引，包括 `.env`、临时文件、大文件。Agent 应该明确知道自己在提交什么。

**更彻底的一层是工具 schema**：`git_push` 工具只接受一个 `branch` 参数，不给任意 flag。

> 工具 schema 只收 `branch`，不给任意 flag：结构上就绕不开「禁强推/禁删远端分支」。

这是"用接口形状限制能力"的典型手法——比在实现里逐个过滤 flag 更难出错。

### 5.7 凭据不出可信侧

Git 推送是最容易泄密的一环。传统做法是把 token 塞进沙箱的 `git remote` URL 或 `~/.git-credentials`，这等于把长期凭据放进了模型可以 `cat` 的文件系统。

这个项目的做法是**凭据永远不进入 Agent 可达的环境**：

```
Agent 沙箱                         后端（可信侧）
─────────                         ──────────────
git bundle create ──► bundle 字节 ──► 临时 bare 仓库
（没有任何凭据）                       │  git fetch bundle
                                     │  用 GIT_CONFIG_* 环境变量注入 token
                                     └─► git push
```

注入方式（`gitops/push.py:70`）：

```python
def _auth_env(token: str) -> dict[str, str]:
    """GitHub HTTPS 认证：任意用户名 + token 作密码，经 http.extraHeader 注入。"""
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
    }
```

**为什么用 `GIT_CONFIG_*` 环境变量而不是 `git remote set-url`？**

| 方式 | token 落在哪 | 会被谁看到 |
|---|---|---|
| `remote set-url` | `.git/config` 文件 | 磁盘、Agent、日志 |
| `-c http.extraHeader=...` | **命令行参数** | `ps aux` 能看到 |
| `GIT_CONFIG_*` 环境变量 | 子进程环境 | 只有该子进程 |

环境变量是三者中暴露面最小的：不落盘、不进命令行、不进进程列表。

配套的还有几处细节：

```python
env.update({
    "GIT_TERMINAL_PROMPT": "0",   # 禁凭据提示（否则会挂住等 stdin）
    "GIT_ASKPASS": "true",        # 需要凭据时直接失败而不是弹窗
    "GIT_CONFIG_NOSYSTEM": "1",   # 不读系统级 git 配置（防止被宿主环境干扰）
})
```

`GIT_TERMINAL_PROMPT=0` 的必要性：如果 token 失效，git 会尝试交互式询问用户名密码，在无终端的子进程里**永久挂住**，直到 300 秒超时。

**stderr 也要脱敏**（`gitops/redact.py`）：`_run_git` 返回前对 stderr 做了 `redact_git_output()`，防止错误信息里带出 URL 中的凭据或 token 片段。

**bundle 的一个技术细节**：

```python
# 用 **bare** 临时仓库接收：没有 checked-out 的分支，fetch 到任意
# refs/heads/* 都不会被 git 拒绝（非 bare 时会报
# "refusing to fetch into branch 'refs/heads/<b>' checked out at ..."）。
code, _, err = await _run_git(["init", "--bare", "-q"], tmp, None)
```

以及源分支与目标分支解耦：

```python
# 源分支与目标分支**解耦**：`refs/heads/<branch>:refs/heads/<target>`。
# 这样「本地 master → 远端 main」「本地 master → 远端 docs/readme」都能表达；
# 早先强制同名（x:x）导致任何分支名不一致的场景都推不上去。
```

**分支名校验**（`validate_branch_name`）是模型可控输入，必须挡在拼进命令之前：拒绝 `-` 开头（会被当成 flag）、`..`（路径穿越）、`@{`、空格、`~^:?*[\`、控制字符。

### 5.8 安全设计的三条通用手法

回头看这一章，所有防护都可以归到三种手法：

| 手法 | 例子 | 特点 |
|---|---|---|
| **白名单 + 默认拒绝** | git 子命令、路径顶级目录 | 漏做 → 功能不可用（安全） |
| **接口形状限制能力** | `git_push` 只收 branch；路径必须相对 | 绕不过去，不依赖过滤逻辑正确 |
| **失败可见** | 出口策略下发失败显式告警 | 不会"以为收紧了其实没开" |

第三条最容易被忽略。安全配置的静默失败比没有配置更危险——因为它给了你虚假的安全感。

---

## 6. 状态、恢复与一致性

### 6.1 三张表

| 表 | 一行是什么 | 谁写 |
|---|---|---|
| `chat_messages` | 一条消息（user / assistant） | `SessionStore.add_*_message` |
| `session_meta` | 一个会话的元信息（归档、task_status、workspace） | 状态机、归档操作 |
| `run_checkpoints` | 一次工具调用的进度 | `append_checkpoint`（每步一次） |

`session_meta.task_status` 的三个值：`in_progress` / `completed` / `canceled`。`NULL` 表示从未有过 run。

### 6.2 恢复判定的核心：不写就是没完成

```python
# 任务状态：run 启动即标记 in_progress（后续只有"模型主动完成/用户取消"会改走，
# 其余异常终止一律不改——因此崩溃后状态天然停留在 in_progress，可被恢复）
await self.store.set_task_status(request.user_id, request.session_id, "in_progress")
```

写入路径只有三处：

| 时机 | 写入值 |
|---|---|
| run 启动 | `in_progress` |
| 模型主动收尾 / 轮次耗尽（正常出口） | `completed` |
| 用户点停止 | `canceled` |

**其余所有路径——轮次耗尽、context 预算耗尽、API 超时、网络中断、进程被 `kill -9`、容器被 OOM——全都不写。**

于是"崩溃"不需要检测：状态躺在 `in_progress` 里就说明没走完。没有 watchdog、没有心跳表、没有启动时的僵尸扫描。

这是把**检测问题转化成了默认值问题**。

对比错误做法：

```python
# 反例：按终止原因判断
if end_reason in ("timeout", "rounds_exhausted", "process_killed"):
    inject_resume_context()
```

三个问题：① 每新增一种终止方式就要改这里，漏一个就静默失去恢复能力；② `process_killed` 根本不会产生 `end_reason`，分支永远进不去；③ 把"诊断标签"当成了"控制流依据"。

正确判定只有两条，与原因无关：

```python
status = await self.store.get_task_status(user_id, session_id)
if status != "in_progress":
    return
ckpt = await self.store.load_latest_checkpoint(user_id, session_id)
if not ckpt:
    return
```

### 6.3 进度日志：WAL 思路

`run_checkpoints` 每步一行：

```sql
CREATE TABLE IF NOT EXISTS run_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    step INTEGER NOT NULL,          -- 第几次工具调用
    tool TEXT NOT NULL,
    args_digest TEXT,               -- 参数摘要
    result_digest TEXT,             -- 结果摘要
    progress TEXT,                  -- 结构化进度（JSON）：如 read_file 的 {path,startLine,endLine,totalLines}
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```

三个设计点：

**① 每次工具调用成功后立即写，不等 run 结束。** 等 run 结束才写的话，崩溃时进度全丢——而崩溃恰恰是最需要进度的时刻。

**② 存摘要而不是全文。** `args_digest` / `result_digest` 只存前 300 字符。全文存进去，一个读文件的 checkpoint 就能有几十 KB，几十步下来表就爆了。

**③ `progress` 是结构化的。** `read_file` 记录 `{path, startLine, endLine, totalLines}`，恢复时能按 path 聚合出"已读过哪些文件、读到第几行"。

### 6.4 恢复注入：带具体进度，不说"继续"

```python
note = (
    f"\n\n【续接提示】上一次任务在未完成的状态下中断（已执行 "
    f"{ckpt['step']} 步工具调用，最后一步：{ckpt['tool']}）。"
    "请基于已有上下文继续完成剩余部分，不要从头重复已完成的工作。"
)

reads = await self.store.load_read_progress(request.user_id, request.session_id)
if reads:
    seen: dict[str, dict] = {}
    for r in reads:
        path = r.get("path", "")
        prev = seen.get(path)
        if prev is None or (r.get("endLine") or 0) > (prev.get("endLine") or 0):
            seen[path] = r                      # 同一文件只保留读得最远的那次
    items = list(seen.values())[-20:]           # 最多列 20 个文件
    lines = [f"- {r['path']}（已读 {r['startLine']}-{r['endLine']}/{r['totalLines']} 行）" ...]
    note += "\n已读文件（不必重读，除非需要确认）：\n" + "\n".join(lines)

configured.system_instruction += note
```

**为什么不只说"请继续"？** 因为模型会从头重扫一遍。每个文件一次沙箱往返，真实 E2B 单次读取 2~9 秒，十几个文件就是一两分钟纯浪费。告诉它"这些已经看过了"，能省掉一半时间。

**为什么按 path 聚合而不是直接列出所有记录？** 同一个文件可能被读多次（不同区间），全列出来既浪费 token 又让模型困惑。只保留读得最远的那次，语义最清晰。

**为什么上限 20 个？** 超过 20 个文件的列表本身就很长，占用的 token 可能比重新读几个小文件还多。

### 6.5 诊断与控制流必须分离

这一节是整个项目最重要的架构原则，在四处独立实现：

| 场景 | 诊断信息 | 控制流依据 |
|---|---|---|
| 任务恢复 | `end_reason`（completed / rounds_exhausted / timeout...） | `task_status` + `checkpoint` |
| 用量统计 | `tokens_estimated`、`reported` | 累加逻辑 |
| 链路追踪 | trace step 的耗时 | 不参与任何分支 |
| 工具错误 | `kind`（tool_failure / command_error） | 熔断计数只看 `retryable` |

混在一起的代价：**每加一种新情况都要回头改旧逻辑**，而且旧逻辑的分支会越加越多，最终没人敢删。

`end_reason` 的全部职责就是让日志和 UI 能说清楚"为什么结束了"：

```python
logger.info(
    "agentrun end request_id=%s duration_ms=%.2f tokens=%d mcp_tools=%d "
    "tools_called=%d reason=%s end_reason=%s",
    ...
    end_reason,  # 终止诊断（与恢复依据 task_status 解耦）
)
```

---

## 7. 工作区存储与同步

### 7.1 工作区是什么

一个工作区（workspace）= 一组文件 + 一份元信息（名称、git remote、指令、自动记忆、出口白名单）。沙箱里挂在 `workspace/{workspace_id}/` 下。

沙箱内还有四个固定顶级目录（`sandbox/workspace.py:15`）：

```python
ALLOWED_BASE_DIRS = {"uploads", "work", "artifacts", "workspace", "skills"}
```

| 目录 | 用途 |
|---|---|
| `uploads/` | 用户上传的附件 |
| `work/` | 命令执行的默认 cwd |
| `artifacts/` | Agent 产出的可下载产物 |
| `workspace/{id}/` | 工作区文件（唯一按 id 隔离的目录） |
| `skills/` | Skill 资源 |

### 7.2 两种存储后端

`EDITH_WORKSPACE_VOLUME_BACKEND` 决定文件权威副本在哪：

```
VolumeBackend (ABC)
├── E2BVolumeBackend   权威副本 = E2B Volume（云端多租户的正确答案）
└── LocalVolumeBackend 权威副本 = 本机目录（开发/自建部署）
```

`new_backend(kind, store)`（`volume_backend.py:315`）是唯一的分叉点。

**为什么保留两种而不是只留 E2B？** 两个原因：

1. **E2B Volume 目前是 private beta**，需要向 support@e2b.dev 申请。`AsyncVolume.create` 在未开通的账号上稳定返回 `403: use of volumes is not enabled`（`list` 可用、沙箱正常）。没有 local 后端，整个项目就跑不起来。
2. **自建部署场景**下，用户希望文件就在自己机器上，不想过第三方存储。

这是一个"用接口隔离不确定性"的典型例子：上层（`WorkspaceVolumeService`）完全不知道自己用的是哪种后端。

### 7.3 同步层：为什么 local 模式必须有三条链路

在 E2B 后端下，沙箱通过**可写挂载**直接读写 Volume，**不需要同步**。但在 local 后端下，本地目录和沙箱是两份独立副本，于是需要三条链路：

```
B1  本地 → 沙箱   初始灌入 / 沙箱重建后重灌
B2  沙箱 → 本地   回拉：Agent 改了什么，本地要跟上
B3  本地 → 沙箱   推送：本地改了什么，沙箱要跟上
```

**B2 的触发时机是关键**：它在**每个写类工具调用之后**都会跑，而且在**工具返回之前**同步等待（`sandbox/tools.py:396-398`）。

```python
_MUTATING_TOOLS = {
    "sandbox_write_file", "sandbox_run_command",
    "sandbox_make_directory", "sandbox_move_path", "sandbox_delete_path",
}
```

这意味着 **B2 的每一次浪费都是用户能感觉到的卡顿**。所有性能护栏都是围绕这一点设计的。

### 7.4 性能护栏：82.4 秒 → 2.7 秒

代码里的编号（①②④⑤）是显式标注的，说明这些是有意设计的五道护栏：

| # | 护栏 | 做法 | 不做的后果 |
|---|---|---|---|
| ① | 免下载比对 | `files.list` 的 size 一致就不读内容 | 13 个文件 13 次远端读 |
| ② | 单文件体积上限 | `EDITH_SYNC_MAX_FILE_BYTES`（2 MiB） | 一个 50MB 的 zip 卡死整轮同步 |
| ③ | 二进制后缀排除 | `BINARY_SUFFIXES` + `EDITH_SYNC_PULL_BINARY_FILES` | 图片走文本比对，必然不一致 |
| ④ | 连续失败熔断 | `EDITH_SYNC_FAILURE_LIMIT`（3） | 网络断了逐文件各等一个超时 |
| ⑤ | 显式超时 | `request_timeout=SYNC_READ_TIMEOUT_S`（15） | 落到 SDK 默认 60s |

第 ⑤ 条的必要性值得单独强调：

```python
# ⑤ 显式短超时：绝不用 SDK 默认的 60s 整请求硬超时
raw = await sandbox.files.read(entry.path, format="bytes", request_timeout=read_timeout)
```

E2B SDK 默认是 **60 秒整请求硬超时**。不显式设，一次卡顿就是 60 秒；结合熔断（3 次）最坏耗时是 `3 × 15 = 45 秒`，而不是 `3 × 60 = 180 秒`。

第 ④ 条的注释也很清楚：

```python
# ④ 熔断：连续失败达阈值即中止本轮，避免逐文件各等一个超时
if failures >= failure_limit:
    aborted = True
    logger.warning("B2 sync aborted after %d consecutive failures ws=%s: "
                   "剩余文件本轮不再尝试（下轮重试）", ...)
    break
```

注意 `failures = 0` 只在成功后重置——这是**连续**失败计数，不是累计。偶尔一个文件失败不影响整体。

实测收益（同样 13 个文件）：

| | 远端读取次数 | 耗时 |
|---|---|---|
| 旧 | 13 次 | 82.4 秒 |
| 新 | 3 次 | 2.7 秒 |

### 7.5 删除传播：一个真实事故引出的机制

**事故经过**：Agent 连续三次删除 `mp/agent-learn` 目录，每次都返回 `path deleted`，用户第二天打开一看，**文件还在**。

**根因不是 E2B，是同步层只做 upsert、从不删对侧**，形成双向复活死锁：

```
本地删了 → B2 回拉"本地有什么就拉什么"，不会删沙箱 → 沙箱侧复活
沙箱删了 → B3 推送"沙箱有什么就推什么"，不会删本地 → 本地侧复活
```

更糟的一层：`sandbox_delete_path` 本身在 `_MUTATING_TOOLS` 里，执行前会先跑一次 B3 推送——**先把本地副本推回沙箱，再执行删除**，等于白删。

**解法不是给每条删除路径打补丁，而是换一个模型。**

维护「两侧共识文件集合」`synced`（落盘 `workspace-sync/{workspace_id}.json`），每次同步只做一次集合差：

```
B2 回拉：本地该删的 = synced − 沙箱当前集合
B3 推送：沙箱该删的 = synced − 本地当前集合
```

关键：**不按删除来源写任何分支**。工具删除、`run_command rm`、`git checkout` 回滚、用户在 IDE 里手工删——全部统一收敛成"集合里少了什么"。

```python
if SYNC_PROPAGATE_DELETES:
    save_synced_paths(workspace_id, synced, sandbox_id, state_dir)
```

注意集合是每次同步后**重新保存**的，所以它总是反映"上一次双方一致的状态"。

### 7.6 五道误删护栏

删除传播比同步危险得多，所以配了五道：

| 护栏 | 实现 | 防的是什么 |
|---|---|---|
| 无基线不传播 | 首次同步只建基线，不做删除 | 第一次不知道谁对谁错 |
| 沙箱重建不传播 | `sandbox_id` 变化时 B2 跳过删除 | 新沙箱是空的 ≠ 用户删光了 |
| 单轮上限 | `SYNC_MAX_DELETES`（500） | 异常状态不会一次删光 |
| 整体消失只告警 | `_is_suspicious_full_wipe()` | 盘没挂载 ≠ 用户删光了 |
| 独立开关 | `EDITH_SYNC_PROPAGATE_DELETES` | 出问题能一键退回纯 upsert |

第四道最容易被低估：

```python
def _is_suspicious_full_wipe(vanished: set[str], synced: set[str]) -> bool:
```

如果 Volume 挂载失败，沙箱里一个文件都没有，集合差算出来就是"synced 里的全部都要删"——一次误判清空整个工作区。所以必须有一条"**消失得太彻底就不像真的**"的判断。

### 7.7 `files.remove` 是递归的

E2B 的 `files.remove` **对目录是递归删除的**。所以回收空父目录时必须先 list 确认为空：

```python
async def _prune_empty_parents(sandbox, file_path) -> list[str]:
    """删除文件/目录后回收空父目录，上溯到 workspace/{id} 根为止。

    为什么需要：`sandbox_delete_path mp/agent-learn` 只会删掉子目录，父目录 `mp`
    留成空壳，文件列表里这个名字还在 —— 用户与 Agent 都容易误判为「没删掉」。

    安全性：E2B `files.remove` 对目录是**递归删除**的，所以必须先 list 确认为空
    才能删；否则会把父目录下其它未参与本次删除的文件一起删掉。
    """
    while current.startswith(root + "/"):
        remaining = await sandbox.files.list(current, depth=1)
        if remaining:
            break                      # 还有东西，不能再往上删
        await sandbox.files.remove(current)
```

这个"空壳目录"问题是真实存在的：删了 `mp/agent-learn` 之后 `mp/` 还在，文件面板里能看到 `mp`，用户以为没删掉。同时 `_workspace_root_of()` 保证上溯不会越过 `workspace/{id}` 边界。

### 7.8 一个工作区一个会话

这是消灭"第二个写者"的结构性方案。

**先理解问题**：每个会话持有自己的沙箱，而 B2/B3 都以「本地目录 ↔ 沙箱」为镜像轴。同工作区两个会话即使**不并发**，旧沙箱下次被唤醒时也会把过期内容同步回去（后同步者胜，另一份改动静默丢失）。

```
会话甲的沙箱 A：读到 README v1，改成 v2，同步到本地（本地 = v2）
会话乙的沙箱 B：还持着 README v1 的快照
第二天乙唤醒 → B2 回拉 → sandbox B 有 v1，本地有 v2 → 尺寸不同 → 写成 v1
```

甲的修改**静默蒸发**，没有任何错误提示。

**为什么进程内锁救不了**：`workspace_lanes` 只能挡"同时使用"，挡不住这种**跨时间**的顺序覆盖。锁释放后，问题照样发生。

**解法：DB 层部分唯一索引。**

```sql
CREATE UNIQUE INDEX IF NOT EXISTS session_meta_active_workspace_index
    ON session_meta (user_id, workspace_id)
    WHERE archived = 0 AND workspace_id IS NOT NULL
```

`schema.py` 里的注释把理由写得很完整：

```python
# 为什么必须是结构约束而不是并发闸门：每个会话持有自己的沙箱，而 B2/B3 都以
# 「本地目录 ↔ 沙箱」为镜像轴。同工作区两个会话即使**不并发**，旧沙箱下次被唤醒时
# 也会把过期内容同步回去（后同步者胜，另一份改动静默丢失）。
# `agentrun/workspace_lanes.py` 只挡住了「同时使用」，挡不住这种顺序覆盖。
# 放在唯一索引里，Web / IM / 定时任务 / 直连 API 都绕不过去。
#
# 部分索引条件 archived = 0：归档的会话保留数据但退出索引，所以「归档 → 新开」
# 这条路径仍然成立。workspace_id IS NULL 排除普通对话（它们可自由多开）。
```

三个条件各自的必要性：

| 条件 | 不加会怎样 |
|---|---|
| `archived = 0` | 归档的会话永久占坑，用户归档后再也开不了新会话 |
| `workspace_id IS NOT NULL` | 普通对话（不属于任何工作区）被限制成只能有一个 |
| `user_id` 在最左 | 不同用户的工作区互不影响 |

**迁移三步顺序不可换**：

```python
async def _enforce_single_session_per_workspace(db):
    await db.execute(_BACKFILL_WORKSPACE)                       # 1. 回填 workspace_id
    await _archive_duplicate_workspace_sessions(db)             # 2. 收敛历史重复
    await db.execute(_UNIQUE_ACTIVE_SESSION_INDEX)              # 3. 建索引
```

| 颠倒 | 后果 |
|---|---|
| 先建索引再回填 | 所有行 `workspace_id` 为 NULL，索引失去意义 |
| 先建索引再收敛 | 存量有重复，建索引直接失败 |

重复会话的收敛策略是"**保留最近有消息的那个**，其余归档"——归档不删数据，仍可从侧边栏归档区找回。

**两层防线，各有分工**：

| 层 | 实现 | 失败表现 |
|---|---|---|
| 内存 | `workspace_lanes.acquire` | 返回 `workspace_busy` + 占用者会话 ID，前端可跳转 |
| 持久 | 部分唯一索引 | `WorkspaceSessionConflict` → `workspace_session_conflict` 错误 |

内存层给**好的错误语义**，DB 层给**不可绕过的保证**。只有 DB 层的话，前端只能看到一个 500。

前端也配合了这一点（`lib/chat/workspace-session.ts`）：

```typescript
// 一个工作区只允许一个会话（后端用 session_meta 的部分唯一索引保证）。
// 前端在「点新会话 / 进入工作区」前先问这里，已有就直接打开，而不是造第二个沙箱
// 去覆盖同一份目录镜像。
export async function fetchActiveSessionID(workspaceId: string): Promise<string>
```

**把冲突提前变成一次跳转**，而不是让用户点了新会话再看到报错。

---

## 8. 数据模型

### 8.1 表清单

| 表 | 主键 / 索引 | 一行是什么 |
|---|---|---|
| `chat_messages` | `id` 自增；(user_id, session_id, id) 索引 | 一条消息 |
| `session_summaries` | (user_id, session_id) | 会话摘要 + 折到哪条 |
| `session_meta` | (user_id, session_id) | 归档状态、task_status、workspace_id |
| `run_checkpoints` | `id` 自增；(user_id, session_id, id) 索引 | 一次工具调用的进度 |
| `usage_runs` | request_id | 一次 run 的 Token 用量 |
| `trace_traces` | request_id | 一次 run 的链路骨架 |
| `trace_steps` | (request_id, seq) | 一个步骤 |
| `session_meta` 唯一索引 | (user_id, workspace_id) WHERE archived=0 | 工作区活跃会话不变式 |
| `user_custom_providers` / `user_custom_models` | user_id + 自定义 ID | 自定义模型 |
| `user_git_credentials` | user_id | Git PAT（加密） |
| `user_sandboxes` | (user_id, session_id) | 沙箱 ID 映射 |
| `cron_jobs` | job_id | 定时任务 |

### 8.2 两个"看不见"的设计

**① `chat_messages.workspace_id` 可空，NULL = 普通对话。**

这个字段让"会话属于哪个工作区"有了唯一来源，同时允许"不属于任何工作区的普通对话"存在。回填 SQL 用 `COALESCE` 保证不覆盖已有值：

```sql
INSERT INTO session_meta (user_id, session_id, archived, workspace_id)
SELECT c.user_id, c.session_id, 0, MAX(c.workspace_id)
FROM chat_messages AS c
WHERE c.workspace_id IS NOT NULL
GROUP BY c.user_id, c.session_id
ON CONFLICT(user_id, session_id) DO UPDATE
    SET workspace_id = COALESCE(session_meta.workspace_id, excluded.workspace_id)
    WHERE session_meta.workspace_id IS NULL
```

**② `usage_runs.usage_reported` 可空。**

`NULL` = 该列引入前的旧行，**来源未知**。不能给它 `DEFAULT 1`——那等于给历史估算数据盖上"真实上报"的章。

### 8.3 迁移的写法

`session/schema.py` 里的 `_migrate()` 是加列的先例：

```python
# 加表加列先例：PRAGMA table_info + 幂等 ALTER TABLE ADD COLUMN
```

模式固定：先查 `PRAGMA table_info`，缺列才 `ALTER TABLE ADD COLUMN`，全部幂等。这样启动时无条件跑一遍就行，不需要版本号、不需要迁移框架。

---

## 9. 用量与可观测性

### 9.1 用量采集的三个坑

**坑一：usage 在 `choices` 为空的收尾块里。**

见 §4.2。顺序错了就静默退化成估算。

**坑二：`cached_tokens = 0` 和 `None` 不是一回事。**

| 值 | 含义 |
|---|---|
| `0` | 供应商明确上报"这次缓存命中 0 token" |
| `None` | 供应商没上报（未知） |

累加时**绝不能把 `None` 当 0 求和**，否则系统性低估命中率。`_UsageAccumulator` 的规则是：任一轮缺失即整轮 Unknown。

**坑三：估算值必须显式标记。**

```python
except Exception:  # 供应商流里没有 usage 块
    prompt_est = _estimate_tokens(messages)
    completion_est = _estimate_tokens([{"content": content + reasoning}])
    llm_step.metadata["tokens_estimated"] = True
    usage_acc.add(Tokens(
        prompt_tokens=prompt_est,
        cached_prompt_tokens=None,      # ← 估算不含缓存字段
        completion_tokens=completion_est,
        total_tokens=prompt_est + completion_est,
        reported=False,                 # ← 显式标记
    ))
```

`Tokens` 的注释说明了为什么：

> 估算值也计入总量，但必须可与真实上报区分，否则「看起来精确的假数据」比缺数据更危险。

### 9.2 汇总口径：分子分母同源

`Summary` 的注释解释了旧实现的错误：

```python
"""一个会话的浏览器可展示用量汇总。

缓存类字段按「已上报缓存的全集子集」聚合：只要有一次运行没上报，
就只统计上报过的那些运行，并用 cache_reported_runs/total_runs 说明覆盖度，
而不是整会话一起变成未知（旧实现的全有或全无语义会让一次估算废掉整个会话）。
"""
```

| 字段 | 含义 |
|---|---|
| `total_runs` | 计入统计的已完成运行数 |
| `cache_reported_runs` | 其中真实上报了缓存字段的运行数 |
| `estimated_runs` | 其中用量由本地估算的运行数 |

命中率的计算只在 `cache_reported_runs` 子集上做，**分子分母同源**。同时下发覆盖率，让用户知道这个数覆盖了多少比例的运行。

### 9.3 耗时口径：只测一次

```python
# started_at：本轮任务起点（time.monotonic）。由调用方传入以覆盖 MCP 连接等准备阶段；
# 缺省则以本函数入口为起点。耗时随 assistant 消息一并落库，刷新历史仍可见。
```

`started_at` 在 `_run()` 入口就记下，传给 `chat()`，出口测一次：

```python
duration = elapsed_ms(started_at)
await store.add_assistant_message(..., duration)
```

然后这个值**既随 SSE 终态事件下发，也落库**。所以界面上的实时值和刷新后的历史值来自同一个数。

如果两处分别计算（实时用前端计时，历史用后端耗时），用户会看到"刚才显示 3.2s，刷新后变成 1.8s"。

### 9.4 序列化不对称：一个必须知道的坑

同一条数据，两个接口的序列化规则不同：

| 接口 | 规则 | 无值时的表现 |
|---|---|---|
| SSE 事件 | `model_dump_json(exclude_none=True)` | 字段**整个缺失** → 前端 `undefined` |
| 历史接口 | FastAPI `response_model` 默认 | 字段存在但为 `null` |

所以前端判空**只能用 `typeof x === "number"`**：

```typescript
// 实时事件用 exclude_none（无耗时即缺字段），历史投影则可能给 null——两者都视为"没有"。
function stampDuration(timeline, assistantID?: string, durationMs?: number | null) {
  if (!assistantID || typeof durationMs !== "number") {
    return timeline;
  }
  ...
}
```

如果写 `durationMs !== undefined`，`null` 会漏过去，渲染出 `"nullms"`；如果写 `!== null`，`undefined` 会漏过去，渲染出 `"NaNms"`。

### 9.5 trace：记录一次 run 的完整形状

数据结构（`trace/types.py`）：

```python
@dataclass
class TraceStep:
    type: str                  # PREPARE / MCP / LLM_CALL / TOOL_CALL / RESUME / ...
    name: str
    seq: int = 0
    parent_seq: int | None = None   # None = 顶层
    depth: int = 0
    duration_ms: float = 0.0
    status: str = OK
    input_digest: str | None = None     # 摘要，不是原文
    output_digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Trace:
    request_id: str
    status: str = RUNNING
    end_reason: str | None = None
    duration_ms: int | None = None      # 墙钟
    self_time_ms: int | None = None     # 顶层 step 之和
    degraded: bool = False
    degradations: list[dict[str, str]] = field(default_factory=list)
```

**两个字段值得单独说：**

```python
def unattributed_ms(self) -> int | None:
    """墙钟 - 自耗时 = 尚未打点的时间（驱动下一步埋点）。"""
    if self.duration_ms is None or self.self_time_ms is None:
        return None
    return max(self.duration_ms - self.self_time_ms, 0)
```

这个值直接告诉你"还有多少时间没被解释"。新功能上线后看这个数，就知道该往哪儿补埋点——**打点覆盖率是可度量的，不是靠感觉**。

`degraded` / `degradations` 是降级的显式记录：

```python
def degrade(self, kind: str, from_: str, to: str, reason: str = "") -> None:
    """记录一次降级（ETHOS 的降级必须显式可见，不能静默）。"""
```

三个降级场景：E2B 挂了转纯文本、模型沿回退链降级、同步失败跳过。每一个都必须可见，否则用户会觉得"今天怎么傻了点"却查不出原因。

**打点的两种落盘时机**：

```python
async def begin(self):
    """写入骨架行（立即 commit：让「正在进行」立刻可见）。"""

async def flush(self):
    """把缓冲中已完成的步骤批量落盘（一次 commit）。"""
```

骨架立即提交，步骤批量提交（每轮结束一次）。既保证"进行中"可查，又避免每步一次 fsync。

### 9.6 离线查看器

`trace/viewer.py` 是一个 CLI：

```bash
python -m edith_backend.internal.trace.viewer list      # 列出最近 run
python -m edith_backend.internal.trace.viewer show <id> # 看单个 run 的步骤树
python -m edith_backend.internal.trace.viewer slow      # 按耗时排序找慢请求
```

`slow` 子命令是日常排查的主力：不用翻日志，直接按耗时倒序列出慢 run。

**trace 保留期**默认 14 天（失败/降级的 run 内部翻倍），启动时清理：

```python
removed = await traces.cleanup(int(os.getenv("EDITH_TRACE_RETENTION_DAYS", "14")))
```

多租户下不清理会无限增长。

### 9.7 request_id 贯穿

一个 ID 串起四层：

```
前端 crypto.randomUUID()
  → BFF 的 X-Request-ID header
  → 后端 ContextVar（日志自动注入 [request_id]）
  → trace 主键 / usage 主键 / assistant_id
```

日志配置：

```python
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")

class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id_var.get("no-request")
        return True

# 关键：filter 必须挂在 root 的 handler 上，而不是 root logger 上。
# logging 的 logger 级 filter 只对「发出日志的 logger 自身」生效；
# 子模块 logger 的 record 传播到 root 后由 handler 处理，handler 级 filter
# 才对所有经过的 record 生效（含第三方库日志）。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIDFilter())
```

这个 "filter 挂在 handler 而不是 logger 上" 的细节，是一个典型的 Python logging 陷阱。挂在 logger 上，只有直接调用该 logger 的日志有 request_id，所有子模块的日志都没有。

---

## 10. 前端链路

### 10.1 BFF：44 个转发路由

`app/api/**/route.ts` 全是薄转发层，模式统一：

```typescript
const { userId } = await auth();
if (!userId) return 401;
// 转发到 runtimeURL/internal/...
```

三个职责：

| 职责 | 说明 |
|---|---|
| 认证 | 从 Clerk session 解出 userId |
| 白名单校验 | 字段类型、必填、UUID 格式 |
| 透传 | 把 userId 拼进请求体，转发到后端 |

**为什么每个路由都要重复这段？** 因为它们各自有独立的字段校验逻辑，抽公共函数会牺牲清晰度。代价是新增字段要改两处（见 §3.3 的 workspaceId 坑）。

### 10.2 SSE 解析：按 `\n\n` 切帧

```typescript
export async function readChatStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });   // ← stream: !done

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      readFrame(frame, onEvent);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) return;
  }
}
```

三个细节：

**① `decoder.decode(value, { stream: !done })`。** 流式解码必须告诉 TextDecoder "这不是最后一块"，否则多字节字符（中文）跨 chunk 边界时会被截断成乱码。结尾时 `stream: false` 让它 flush 残余字节。

**② 缓冲区 + `indexOf("\n\n")` 切帧。** 一个 TCP 包里可能包含半帧、一帧或三帧。必须缓冲到完整帧边界再解析。

**③ 只取 `data: ` 行。** SSE 还有 `event:`、`id:`、`retry:` 字段，这里只用 `data:`，把整个 JSON 放进去。

### 10.3 时间线：一个纯函数的状态机

`applyStreamEvent(timeline, event) → timeline` 是**唯一的 SSE → UI 状态桥**。它没有副作用，输入输出都是纯数据，所以非常容易推理和测试。

| 事件 | 效果 |
|---|---|
| `run.started` | 追加一个空的 assistant 块 |
| `reasoning.delta` / `message.delta` | 找 blockId，有则追加内容，无则新建块 |
| `tool.started` | 追加一个 tool 块（status=running） |
| `tool.finished` | 按 toolCallId 更新 status 和 result |
| `run.completed` / `run.canceled` | 把 durationMs 挂到 assistant 块上 |
| `run.error` | 先落错误卡片，再把耗时挂到 assistant 块 |

块 ID 的生成在**后端**（`agentstream/decoder.py`）：

```python
def content_delta(self, block_type, event_type, delta):
    """同一类型的连续增量共享同一个 BlockID；类型切换时分配新块。"""
    if self.last_type != block_type:
        self.next_block_id += 1
        self.last_type = block_type
        self.last_id = f"{self.assistant_id}_{block_type}_{self.next_block_id}"
    return Event(..., block_id=self.last_id, ...)

def tool_started(self, tool_call_id, ...):
    self.end_content_block()      # ← 工具调用强制结束当前内容块
    ...
```

`end_content_block()` 是关键：模型输出"我先读一下文件"然后调工具，工具返回后又输出"文件里写的是..."，这应该是**两个独立的文本块**，不是一段。没有这个重置，两段文字会被拼成一段，中间夹着工具卡片。

### 10.4 实时 vs 刷新后：两套块 ID 与两种顺序

这是一个**已知的呈现差异**，值得理解原因。

**块 ID 不同**：

| | assistant ID | 块 ID |
|---|---|---|
| 实时 | `assistant_{request_id}` | `{assistant_id}_{type}_{n}` |
| 刷新后 | `assistant_{db_id}` | `assistant_{db_id}_reasoning` / `_tool_{id}` / `_text` |

**块顺序不同**：

| | 顺序 |
|---|---|
| 实时 | 按事件到达顺序（reasoning 先，text 与 tool 交错） |
| 刷新后 | 固定 reasoning → tools → text（`history.py:_timeline`） |

为什么可以接受？因为刷新时整个 timeline 被**整体替换**，不存在"新旧块对比"的场景。ID 不同只影响 React 的 key 变化（会重建 DOM），不影响正确性。

但如果将来要做"页面切走再切回、保留滚动位置"这类体验，这个差异就会变成问题——块 ID 变了，滚动锚点就丢了。

### 10.5 pending run：断网与刷新的恢复

前端在 sessionStorage 里存"未完成的 run"：

```typescript
const pendingRunsKey = "edith.pending-agent-runs";
type PendingRun = { requestId: string; sessionId: string };
```

页面加载时启动 3 秒轮询（`chat-page.tsx:185`）：

```typescript
async function recoverCompletedRuns() {
  for (const pendingRun of loadPendingRuns()) {
    const statusResponse = await fetch(`/api/agent-runs/${pendingRun.requestId}`);

    if (statusResponse.ok) {
      const run = await statusResponse.json();
      if (run.status !== "running") continue;
      // 后端还在跑 → 只提示"流断开"，不动状态
      setSessionRunNotice({ sessionID, reason: "stream_disconnected" });
      continue;
    }

    // 404 = 后端已经没有这个 run 了 → 去拿历史，用最终结果覆盖
    const conversationResponse = await fetch(`/api/conversations/${pendingRun.sessionId}`);
    if (conversationResponse.status === 404) {
      removePendingRun(pendingRun.requestId);   // 会话也没了 → 记录失效
      continue;
    }

    // 会话仍在实时流式输出时，不拿历史覆盖，避免与 SSE 事件竞争丢块
    if (liveStreamSessionIDs.current.has(pendingRun.sessionId)) continue;

    setSessions(...);         // 用历史覆盖
    removePendingRun(pendingRun.requestId);
  }
}
```

**核心逻辑：404 是"任务已结束"的信号。**

后端 `self.active` 字典只保存运行中的 run。进程重启或任务完成后，查询返回 404。所以：

- **200 + running** → 后端还在跑，流断了但任务没断，只提示
- **404** → 后端没这个 run 了，去拿最终结果

注释里那句"会话仍在实时流式输出时，不拿历史覆盖，避免与 SSE 事件竞争丢块"很关键。如果这里不加判断，正在流式的会话会被历史请求的结果覆盖，丢掉还没到的增量。

### 10.6 空会话：一个白屏事故的修复

`refreshConversations` 里有一大段注释：

```typescript
// 新用户（或会话被删光）时后端返回空数组。绝不能把 sessions 清空 —— 那样
// activeSession 会变成 undefined，渲染时读 `activeSession.title` 直接抛错，
// 表现就是 Next.js 全局错误页「This page couldn't load」且没有 digest。
if (history.length === 0) {
  setSessions([emptySession]);
  setActiveSessionID(emptySession.id);
  return history;
}
```

根因链：空列表 → `sessions=[]` → `activeSession` undefined → 渲染读 `.title` → TypeError → Next.js 全局错误页。

**本地复现不出来的原因**：本地库总至少有一个会话。这个 bug 只在"线上全新账号"或"删光所有会话"时出现。

**诊断方法**（很实用）：Next.js 的 `global-error` 页面，`Back` 按钮只在 `!digest` 时出现，有 digest 时页脚会打印 `ERROR <digest>`。所以：

| 现象 | 结论 |
|---|---|
| 有 Reload + Back，页脚无 `ERROR xxx` | 客户端异常（渲染时抛错） |
| 页脚有 `ERROR xxx` | SSR / 服务端报错 |

这个判据能一秒区分两类问题，不用去看日志。

### 10.7 移动端

`<768px` 时侧边栏完全隐藏（`app-sidebar.tsx` 的 `hidden ... md:flex`），文件面板固定 320px 无响应式分支。手机上是**可用但很难用**的状态。

适配方案已存档在 `docs/mobile-adaptation-plan.md`，**尚未实施**。

---

## 11. 部署与运维

### 11.1 拓扑

```
公网 ──► Cloudflare ──► cloudflared 隧道 ──► nginx ──► web (Next.js :3000)
                                    │                    │
                                    │                    ▼
                                    └──────────► backend (FastAPI :8080)
                                                         │
                                                         ▼
                                                   SQLite (volume)
```

启动：`docker compose --profile tunnel up -d`

`tunnel` 服务在 `profiles: ["tunnel"]` 里，所以不带 `--profile` 时不会启动。这是有意的：自建局域网部署不需要隧道。

### 11.2 两个踩过的坑

**坑一：境内节点 + 未备案域名 → 只能走出站隧道。**

未备案域名在 80/443 上会被链路拦截。HTTP-01 证书校验要求"多视角验证"，任一视角失败整体失败，所以 Let's Encrypt 签不出来。走出站隧道（cloudflared 主动连出去）绕开了入站限制。

**坑二：`cloudflared` 必须固定 `--protocol http2`。**

```yaml
command: tunnel --no-autoupdate --protocol http2 run --token ${CLOUDFLARE_TUNNEL_TOKEN:-}
```

默认的 QUIC 走 UDP 7844，在受限出网环境连不上。表现是"隧道一直 connecting"。

**坑三（配置）：token 归属。**

`Unauthorized: Invalid tunnel secret` 这个报错，**第一步应该解码 token 比对 tunnel id，而不是数长度**：

```bash
awk -F'=' '/^CLOUDFLARE_TUNNEL_TOKEN=/{print $2}' .env | cut -d. -f2 | base64 -d
# 看解出来的 t 字段是否等于当前隧道 ID
```

踩坑记录：token 格式完全正常（len=184、head=eyJ、base64 可解），但解码出 `t = b5a192c1-...`，而当前隧道是 `58eee9eb-...` —— token 属于另一条隧道。

### 11.3 nginx：`X-Forwarded-Proto` 必须透传

```nginx
map $http_x_forwarded_proto $edith_forwarded_proto {
    default $http_x_forwarded_proto;
    ""      $scheme;
}
```

**为什么不能直接写 `$scheme`？** 因为 Cloudflare → nginx 这一跳是 HTTP（隧道终点），`$scheme` 永远是 `http`。如果后端用它判断"是不是 HTTPS"，就会把所有 HTTPS 用户当成 HTTP，导致 secure cookie 不生效、重定向循环。

正确做法：如果上游带了 `X-Forwarded-Proto` 就用它，没带才 fallback 到 `$scheme`。

### 11.4 数据在哪，什么操作会丢

| 操作 | 数据影响 |
|---|---|
| `docker compose restart` | 不丢 |
| `docker compose down` | **不丢**（volume 保留） |
| `docker compose down -v` | **全部丢失**（删 volume） |
| 删容器 | 不丢 |
| 重建镜像 | 不丢 |

**永远不要加 `-v`。** 这一条应该写进肌肉记忆。

### 11.5 备份：为什么不能用 `cp`

SQLite 开了 WAL 模式，最近的写入在 `-wal` 文件里，还没 checkpoint 回主库。

```bash
# 错误：得到的是一个"看起来是空表"的数据库
cp edith-v2.db backup.db
```

正确做法是用 SQLite 的在线备份协议（`deploy/backup.sh`）：

```python
# 镜像基于 python:3.13-slim，没有 sqlite3 CLI
conn = sqlite3.connect(src)
dst = sqlite3.connect(dst_path)
conn.backup(dst)      # 在线备份，自动包含 WAL 里的内容
```

`backup.sh` 还有两个必要的防护：

```bash
# 先检查容器在跑，否则备份到的是被占用的文件
if [ -z "$(docker compose ps -q backend)" ]; then
    echo "backend container is not running" >&2
    exit 1
fi
```

以及 `PROJECT_DIR` 由脚本位置推导（不硬编码路径），`BACKUP_DIR` / `RETENTION_DAYS` 可用环境变量覆盖。

### 11.6 跨环境迁移：userId 会变

Clerk 的开发实例和生产实例是**两个不同的用户池**：

| 环境 | key 前缀 | userId |
|---|---|---|
| 开发 | `pk_test_` | `user_2abc...` |
| 生产 | `pk_live_` | `user_2xyz...` |

同一个邮箱在两个实例下是两个**不同的 userId**。所以把本地库拷到服务器后，登录进去看到的还是空数据——不是数据丢了，是 `user_id` 对不上。

`deploy/README.md` 里给了改写脚本的思路：先查新环境的 userId，再把旧库里的 `user_id` 全量替换。

---

## 12. 已知边界

诚实地列出来，比假装没有更有价值。

### 12.1 阻塞性

| 边界 | 影响 | 状态 |
|---|---|---|
| E2B Volume 未开通（private beta） | `AsyncVolume.create` 稳定返回 403 | 需向 support@e2b.dev 申请；当前用 `local` 后端 |
| 移动端未适配 | `<768px` 侧边栏隐藏、文件面板固定宽度 | 方案已存档，未实施 |

### 12.2 设计性取舍

| 边界 | 原因 | 风险等级 |
|---|---|---|
| 免下载比对只看 size | 同长度改写不会被同步 | 中（写入通常改长度，但不是必然） |
| SSRF 不解析域名 | 应用层拿不到最终 IP | 中（靠部署层网络策略兜底） |
| `secret.py` 是混淆不是加密 | XOR + Base64，`enc:v1:` 前缀 | **高**（多租户前必须换 KMS） |
| `_extract_and_save_memories` 未计量 | 在 `usage.finish` 之后调用 | 低（少量 token） |
| 实时与历史的块 ID 不同 | 刷新整体替换，不影响正确性 | 低 |
| `run_command rm` 可能留空壳目录 | 绕过工具层，无数据损失 | 低 |

### 12.3 待轮换的凭据

- E2B API key
- Clerk secret
- 两个 GitHub PAT（曾在测试文件里硬编码，已从历史中清理）

### 12.4 延后的深度议题

这些是**已知但有意延后**的，不是遗漏：

| 议题 | 为什么重要 |
|---|---|
| 资源公平与成本隔离 | 一个用户烧光配额会影响所有人 |
| SQLite 单机残留 | 多副本部署时会有写冲突 |
| 提示注入的纵深防御 | 当前只有路径授权 + 敏感路径，内容层面无检测 |
| 审计日志的可信度 | 当前日志可被拥有服务器权限的人修改 |
| 认知状态持久化 | Agent "记得自己在干什么"目前只靠 checkpoint |
| 摘要漂移 | 多次压缩后摘要与原文的偏离没有度量 |
| 不可逆动作的撤销 | 删除、push 都无法回滚 |
| 级联雪崩 | 一个依赖挂掉后所有 run 同时重试 |
| 决策归因与评测 | 无法回答"这个回答为什么是这个回答" |

---

## 13. 设计原则复盘

把整个项目压成 12 条可复用的原则。每条都能在代码里找到对应实现。

### 13.1 让默认值替你检测异常

`task_status` 只在正常出口改，崩溃即停留在 `in_progress`。不需要 watchdog、心跳、僵尸扫描。

**通用形式**：设计状态时问一句"如果这一步没执行完，状态会是什么？"如果答案是"看起来和成功一样"，这个设计就有问题。

### 13.2 诊断与控制流必须分离

`end_reason` 只进日志和 UI，恢复只看 `task_status + checkpoint`。

**通用形式**：任何一个字段，如果它同时被用来"解释发生了什么"和"决定接下来做什么"，就要拆成两个。

### 13.3 结构约束优于运行时检查

工作区唯一会话用 DB 部分索引，不是并发闸门。`git_push` 工具只收 `branch`，不给任意 flag。

**通用形式**：能通过接口形状、类型、约束表达的限制，不要靠过滤逻辑。过滤逻辑会漏，约束不会。

### 13.4 白名单，让失败倒向安全侧

git 子命令、路径顶级目录、出口域名，全是白名单。

> 默认拒绝意味着：漏登记 = 越权，而不是漏拦 = 越权。

**通用形式**：安全判断的默认值必须是"拒绝"。漏做的代价应该是功能不可用（会有人报），不是越权（没人报）。

### 13.5 拒绝要发生在最便宜的位置

参数校验在准入、模型不支持图片在准备阶段拒绝、只读结果有缓存、免下载比对先看 size、熔断在第二次失败时掐断。

**通用形式**：把最贵的操作（模型调用、沙箱往返）放在最后，前面每一层都尽量多拒绝一些。

### 13.6 进度必须在终止之前落盘

checkpoint 写在熔断判断之前，每次工具调用后立即写。

**通用形式**：任何"可能中断"的长流程，进度持久化不能依赖流程正常结束。

### 13.7 收敛成集合运算，不要按来源分支

删除传播不区分"工具删的 / rm 删的 / git 回滚删的 / 手工删的"，统一用集合差。

**通用形式**：当有 N 条路径会导致同一个结果时，不要给 N 条路径各写一段逻辑，而是找那个"结果状态"本身，用状态差驱动。

### 13.8 降级必须显式可见

`tracer.degrade()` 记录每一次降级；`reported=False` 标记每一次估算；出口策略下发失败显式告警。

**通用形式**：任何"退而求其次"的路径，都要留下痕迹。静默降级比不降级更糟——用户会以为系统正常。

### 13.9 蒙混过关的假数据比缺数据更危险

估算 token 必须标 `reported=False`；`None` 和 `0` 严格区分；覆盖率随命中率一起下发。

**通用形式**：不确定的值，要么标成不确定，要么不给。不要用一个"看起来精确"的数字掩盖不确定性。

### 13.10 让"漏做"变成测试失败

```python
# 新增文件类工具时必须同步登记（tests/test_stage10.py 有断言，漏登记会测试失败而不是静默放行）
```

**通用形式**：登记表 + 断言测试。人一定会忘，测试不会。

### 13.11 承认边界比假装全覆盖更可靠

`assert_public_host` 的注释直接写了"非 IP 字面量 → 视为域名，放行（域名解析后的私网 IP 由部署层网络策略兜底）"。

**通用形式**：在代码里写清楚"我这里防不住什么，谁负责兜底"。否则下一个人会以为这里已经全覆盖。

### 13.12 共享能力的位置决定依赖图的形状

`netguard.py` 放在 `internal/` 顶层而不是 `gitops/` 下，因为放进去会让 `models` 被迫加载 e2b。

**通用形式**：一个纯函数被两个模块共用时，它的位置不只是"放哪方便"，而是"会不会拖来不需要的依赖"。

---

## 附录 A：常用命令

```bash
# 后端（本机）
cd backend-py
./.venv/Scripts/python.exe -m uvicorn edith_backend.main:app --reload --host 127.0.0.1 --port 8080
# 注意：uv run uvicorn 在本机会卡住

# 前端
cd web-v1 && npm run dev          # :3000，无 .env 时 Clerk 走 keyless 模式

# 测试
cd backend-py && ./.venv/Scripts/python.exe -m pytest tests/ -q

# trace 查看
python -m edith_backend.internal.trace.viewer slow

# 生产
docker compose --profile tunnel up -d
docker compose --profile tunnel logs -f tunnel
docker compose --profile tunnel down        # 永远不加 -v

# 备份
./deploy/backup.sh
```

## 附录 B：关键环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `EDITH_WORKSPACE_VOLUME_BACKEND` | `e2b` | `e2b` / `local`（当前部署用 local） |
| `EDITH_E2B_TEMPLATE` | `edith-sandbox-python-dev` | 沙箱模板 |
| `EDITH_SANDBOX_ALLOWED_DOMAINS` | 空 | 出口白名单（空 = 不限） |
| `EDITH_SYNC_MAX_FILE_BYTES` | 2 MiB | B2 单文件上限 |
| `EDITH_SYNC_READ_TIMEOUT_S` | 15 | 单次远端读超时 |
| `EDITH_SYNC_FAILURE_LIMIT` | 3 | 连续失败熔断阈值 |
| `EDITH_SYNC_PROPAGATE_DELETES` | true | 删除传播总开关 |
| `EDITH_SYNC_MAX_DELETES` | 500 | 单轮删除上限 |
| `EDITH_SYNC_PULL_BINARY_FILES` | false | 是否回拉二进制文件 |
| `EDITH_TRACE_RETENTION_DAYS` | 14 | trace 保留期 |
| `EDITH_TRACE_FULL` | 未设 | 设 1 则 trace 存原文而非摘要 |
| `EDITH_RUNTIME_URL` | `http://127.0.0.1:8080` | 前端 BFF 指向的后端 |

## 附录 C：端口与关键约定

| 端口 | 服务 |
|---|---|
| 3000 | Next.js（web-v1） |
| 8080 | FastAPI（backend-py） |
| 80/443 | nginx |

- 会话是**隐式**的：按 `session_id` 分组消息，没有独立的 sessions 表
- `session_id` 由前端生成（UUID），不是后端分配
- 一个工作区只能有一个未归档会话（DB 唯一索引）
- 普通对话（`workspace_id IS NULL`）可以自由多开

