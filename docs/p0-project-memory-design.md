# P0 设计稿：项目级记忆（Project Memory）

> 状态：设计稿（未实施）
> 对应：`docs/memory-comparison.md` 优化路线 P0——补「无项目级记忆」的最大 gap。
> 目标：让 Agent 跨会话记住「项目背景 / 架构决策 / 稳定约定」，换会话不丢项目上下文（对齐 wuu 的 `memory/MEMORY.md`）。
> 原则：最小改动、贴合现有架构（模块自治 + 显式注入）、为 P1（索引上限 + 安全扫描）预留位置。

---

## 一、设计决策（为什么这样做）

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 「项目」是什么 | **用户命名空间**：`project_id` 字符串 + 用户手写的记忆内容，**不建完整 projects 实体** | ETHOS 无项目实体；最小改动先落地核心价值，项目元数据（名称/图标）后续再加 |
| 记忆粒度 | 每条记忆 = 一行「可注入的事实」（`project_memories` 表多行） | 对齐 wuu「索引 + 主题文件」的**索引是独立条目**思想；便于增删、避免整体覆盖 |
| 注入方式 | 会话请求可选带 `project_id`，Agent 运行时把该项目全部记忆拼进 system | 无 project_id 的会话行为不变（向后兼容） |
| 安全边界 | P0 先做「注入上限」；安全扫描（威胁模式/不可见 Unicode）留到 P1 | 控制本次改动面；扫描逻辑独立成函数，P1 直接插 |

---

## 二、落库结构（1 张新表）

**位置**：`src/edith_backend/internal/userconfig/schema.py`（新增语句，与 user_agents 同模块归属）

```sql
CREATE TABLE IF NOT EXISTS project_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    content     TEXT NOT NULL,          -- 一条可注入的记忆（如「技术栈：FastAPI+SQLite」）
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, project_id, content)   -- 防止重复写入同一条
);
CREATE INDEX IF NOT EXISTS project_memories_lookup
    ON project_memories (user_id, project_id, id);
```

**关键点**：
- 以 `(user_id, project_id)` 为读写粒度，多用户天然隔离（复用现有 user_id 体系，无跨用户泄漏）
- 不设 `summary` 字段——**记忆条目本身就是一句话**（对齐 wuu「one-line hook」；详细说明由 Agent 在沙箱文件里看，P0 不引入正文文件）
- 主键自增 + UNIQUE 防重，支持幂等写入

---

## 三、接口设计（新增 3 个，都在 userconfig 模块）

### 后端路由：`src/edith_backend/internal/userconfig/project_memory_http.py`（新建）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/internal/project-memories` | query: `userId`, `projectId` | `{ memories: [{id, content}] }` | 列出某项目全部记忆 |
| PUT | `/internal/project-memories` | body: `{userId, projectId, content}` | `{ id, content }` | 新增一条记忆（UNIQUE 防重，重复返回已有） |
| DELETE | `/internal/project-memories/{id}` | path: `id`; query: `userId` | 204 | 删除一条记忆 |

**校验**（对齐现有 `http.py` 风格）：
- `userId` / `projectId` 非空（`invalid_request` 400）
- `content` 非空且 `len(content) <= 500`（防单条过长）
- `extra="forbid"` 拒绝未知字段

**依赖注入**（`main.py` lifespan）：新建 `ProjectMemoryStore(db)`，注入 `agentrun.configuration`（读）和 router（写）。

### 前端入口：设置页新增「项目记忆」区（`components/settings/settings-form.tsx`）

```
┌─ 项目记忆 ─────────────────────────────┐
│ 项目 ID:  [my-project]                │
│ 记忆条目:                              │
│   • 技术栈：FastAPI + SQLite          │  [删除]
│   • 部署：单机，端口 8080             │  [删除]
│  [输入新记忆…]              [添加]    │
└────────────────────────────────────────┘
```

- 前端 BFF 转发（`web-v1/app/api/project-memories/route.ts` 新建，复用现有 forward 模式 + X-Request-ID 透传）
- 前端本地存当前项目 ID（localStorage），聊天页发消息时随请求带上

---

## 四、注入时机（核心改动，1 处）

**位置**：`src/edith_backend/internal/agentrun/configuration.py` 的 `load()`，第 6 步（Skills 摘要）之后、组装 `system_instruction` 之前。

```
# 6.5 加载项目记忆（P0 新增）
project_memory = ""
if request.project_id and self.memories is not None:
    project_memory = await self.memories.load_injectable(request.user_id, request.project_id)

system_instruction = _BASE_IDENTITY + personality
if project_memory:
    system_instruction += "\n\n【项目背景】\n" + project_memory
instruction = run_instruction(system_summaries, user_overview, uploads)
...
```

**`Request` 类型**（`agentrun/types.py`）：新增 `project_id: str = ""` 字段（可选，默认空 = 行为不变）。

**`load_injectable` 实现**（`userconfig/project_memory_store.py`）：
```python
async def load_injectable(self, user_id: str, project_id: str) -> str:
    """读该项目全部记忆，拼接为可注入文本；带硬上限（对齐 wuu ≤200 行）。"""
    rows = await self._db.execute(
        "SELECT content FROM project_memories "
        "WHERE user_id = ? AND project_id = ? ORDER BY id LIMIT 200",
        (user_id, project_id),
    )
    lines = [r["content"] for r in await rows.fetchall()]
    if len(lines) >= 200:  # 超限截断 + 警告（P0 简单版；扫描见 P1）
        logger.warning("project memory truncated user=%s project=%s", user_id, project_id)
    return "\n".join(f"- {line}" for line in lines)
```

**注入语义**：项目记忆作为 system 的独立段落 `【项目背景】`，与 personality、Skills 摘要并列——模型把项目事实当作「稳定背景」而非「本次指令」，避免被当成普通对话内容。

---

## 五、改动清单（汇总）

| 文件 | 改动 |
|------|------|
| `userconfig/schema.py` | + `project_memories` 建表语句 |
| `userconfig/project_memory_store.py`（新建） | `ProjectMemoryStore`：list / add / delete / load_injectable |
| `userconfig/project_memory_http.py`（新建） | 3 个路由（GET/PUT/DELETE） |
| `userconfig/__init__.py` | 导出新组件 |
| `main.py` | lifespan 组装 store + include_router；注入 configuration |
| `agentrun/configuration.py` | `load()` 加第 6.5 步注入 |
| `agentrun/types.py` | `Request` 加 `project_id` 字段 |
| `gateway/types.py` | `IncomingMessage` 加 `project_id` 透传 |
| `webadapter/adapter.py` | `stream_message` 传 `project_id` |
| `web-v1/app/api/project-memories/route.ts`（新建） | BFF 转发（含 X-Request-ID） |
| `web-v1/components/settings/settings-form.tsx` | 「项目记忆」管理区 |
| `web-v1/lib/chat/type.ts`（或 ChatRequest） | 聊天请求带 projectId |

**预计新增约 150~200 行，改动 8 个文件（3 新建）。**

---

## 六、边界与验收

### 验收标准
1. 设置页添加 2 条项目记忆 → 发消息（带 project_id）→ 日志/对话可见 Agent 知道项目背景（如「我记得项目用 FastAPI」）
2. 不传 project_id 的会话 → 行为与现在完全一致（回归无风险）
3. 删除记忆 → 下次请求不再注入
4. 重复添加同一条 → 幂等（UNIQUE 防重）
5. `pytest` 全绿 + `py_compile` + `ruff` 通过

### 明确不做（本期边界）
- ❌ 安全扫描（威胁模式/不可见 Unicode）→ P1
- ❌ 记忆自动提炼（会话结束回写）→ P2
- ❌ 项目实体（名称/图标/成员）→ 后续
- ❌ 正文文件（topic.md 式详细内容）→ 需要时经沙箱读

---

## 七、后续衔接

- **P1**：`load_injectable` 里插入 `_scan_line()`（零宽字符/威胁模式过滤），违规行替换为 `[memory line removed: security]`——函数已预留位置，改动集中在 1 个函数
- **P2**：会话结束时让模型提炼「稳定事实」，经用户确认后调 `add` 写入项目记忆（对齐 wuu Dream 插件）

---

## 八、面试话术（对应 P0）

> 补项目级记忆：新增 `project_memories` 表（user_id + project_id + content），会话请求可选带 project_id，Agent 运行时把该项目记忆作为 system 的「项目背景」段注入，上限 200 条防撑爆。用户可在设置页维护。这样换会话不丢项目上下文——对齐 wuu 的 MEMORY.md 思想，但用结构化表而非文件，多用户天然隔离。安全扫描和自动提炼分别排到 P1/P2。
