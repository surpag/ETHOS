# P0 Phase A 设计稿：项目工作区 + 指令文档化（对齐 Codex/Claude Code）

> 状态：设计稿（未实施）
> 背景：P0 v1（`docs/p0-project-memory-design.md`）已落地「项目 ID + 逐条记忆」，但把数据库概念暴露给了用户（用户要输 projectId、逐条加记忆），违反产品设计原则。
> 本次改造：**把「项目」从技术 ID 变成用户概念，把「记忆」从手动录入变成「文件声明 + 自动学习」**，对齐 Codex（AGENTS.md）与 Claude Code（CLAUDE.md + Auto Memory）的双层记忆模型。
> 关联：`docs/memory-comparison.md`（wuu 对比）、`docs/p0-project-memory-design.md`（v1，被本稿取代交互层）。

---

## 一、设计目标

| 维度 | 改造前（v1） | 改造后（Phase A） |
|------|-------------|------------------|
| 项目标识 | 用户手动输入「项目 ID」 | 用户命名「我的 Python 后端项目」，系统生成 project_id |
| 静态指令 | 用户逐条添加「记忆」 | 一个大的文本编辑区，一次性写完整份项目指令（等价 AGENTS.md） |
| 动态记忆 | 不存在 | Phase B 预留：会话结束 AI 自动总结写入（kind=auto） |
| 用户心智 | 高（理解项目 ID 和记忆） | 低（只需知道「这是我的项目」「写下规则」） |
| 黑盒程度 | 低（用户主动维护） | 高（自动记忆无感积累） |

---

## 二、数据模型（2 张表）

### 2.1 新增 `projects` 表（项目实体）

```sql
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,        -- 系统生成（uuid4().hex）
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,           -- 用户命名，如「我的 Python 后端项目」
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name)               -- 同名项目在同一用户下不允许重复
);
CREATE INDEX IF NOT EXISTS projects_user_index
    ON projects (user_id, project_id);
```

**决策**：项目名可重复（不同用户可有同名），同用户内唯一。`project_id` 完全由系统生成，用户永远看不到。

### 2.2 改造 `project_memories` 表（加 kind 字段）

```sql
CREATE TABLE IF NOT EXISTS project_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'user',  -- 'user'=用户写的项目指令；'auto'=AI 自动总结
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, project_id, kind, content)
);
CREATE INDEX IF NOT EXISTS project_memories_lookup
    ON project_memories (user_id, project_id, id);
```

**迁移策略**（对已有 v1 库）：
- `ALTER TABLE project_memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'`（SQLite 支持，已有行自动填 'user'）
- 用 `PRAGMA table_info(project_memories)` 检查列是否存在，存在则跳过（幂等）

---

## 三、接口设计

### 3.1 项目管理（新建 `project_http.py`）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/internal/projects` | body: `{userId, name}` | `{projectId, name}` | 创建项目（name 非空，同用户重名 409） |
| GET | `/internal/projects` | query: `userId` | `{projects: [{projectId, name}]}` | 列出用户全部项目 |

### 3.2 项目指令（复用 `project_memory_http.py`，改造）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| PUT | `/internal/project-instructions` | body: `{userId, projectId, content}` | `{projectId}` | **整段保存**项目指令（覆盖式，kind='user'） |
| GET | `/internal/project-instructions` | query: `userId, projectId` | `{projectId, content}` | 读取整段指令（编辑区回显） |
| DELETE | `/internal/project-instructions` | query: `userId, projectId` | 204 | 清空指令 |

**核心变化**：`PUT project-instructions` 是**覆盖整段**（先删该 project 的 kind='user' 行，再插入新 content），不是逐条 append。

### 3.3 自动记忆（Phase B 预留，本期只留 kind='auto' 的读写能力）

| 方法 | 路径 | 说明 |
|------|------|------|
| PUT | `/internal/project-memories` | body 加 `kind='auto'`（本期前端不调用，给 Phase B 用） |
| GET | `/internal/project-memories` | 可按 kind 过滤（本期仅内部） |

### 3.4 注入逻辑（`configuration.py` 第 6.5 步，改造）

```
project_instruction = await memories.load_instruction(user_id, project_id)   # kind='user' 整段
auto_memories      = await memories.load_auto_memories(user_id, project_id)  # kind='auto' 列表（本期空）

【项目背景】
<instruction 全文>          ← 用户写的指令，优先
（若有 auto）【项目自动记忆】<每条 - xxx>  ← AI 总结，作为补充
```

**顺序语义**：静态指令（硬规则）在前，自动记忆（动态事实）在后。指令为空则只注入自动记忆（或都不注入）。

---

## 四、前端设计

### 4.1 设置页「项目」区（重写 `project-memory.tsx` → 改名 `project-settings.tsx`）

```
┌─ 项目 ──────────────────────────────────────────────┐
│ 当前项目: [▼ 我的 Python 后端项目        ] [＋新建]   │
│                                                   │
│ 项目指令（Agent 每次对话自动读取，等价 AGENTS.md）：  │
│ ┌───────────────────────────────────────────────┐ │
│ │ # 项目背景                                     │ │
│ │ 技术栈：FastAPI + SQLite                       │ │
│ │ 规范：函数必须带 docstring                      │ │
│ │ 常用命令：uv run uvicorn ... --port 8080        │ │
│ └───────────────────────────────────────────────┘ │
│ [保存指令]                                        │
└───────────────────────────────────────────────────┘
```

- **项目选择器**：下拉列出 `GET /projects`，支持「＋新建项目」（弹窗输入名称）→ `POST /projects`
- **指令编辑区**：一个大 textarea（placeholder 提示「写下技术栈、代码规范、常用命令…」），保存时 `PUT project-instructions` 整段覆盖
- **自动记忆区**（Phase B 后显示）：`GET project-memories?kind=auto` 列表 + 删除按钮——让用户能看到/删除 AI 记的东西（信任边界）
- 选中的 `project_id` 存 localStorage（`ethos.projectId`），聊天页读取

### 4.2 聊天页顶部项目指示（`chat-page.tsx`）

```
[💼 我的 Python 后端项目 ▾]    ← 下拉切换，读取 localStorage + 可改
```

- 读取 `ethos.projectId`（已实现），发消息 body 带 `projectId`（已实现）
- 本期可选增强：项目名显示（需 `GET /projects` 拿 name 映射）——**建议做，否则用户看到的是裸 ID**

---

## 五、改动清单

| 文件 | 改动 |
|------|------|
| `userconfig/schema.py` | + `projects` 表；`project_memories` + `kind` 列（含幂等迁移） |
| `userconfig/project_store.py`（新建） | `ProjectStore`：create / list |
| `userconfig/project_memory_store.py` | `load_instruction` / `save_instruction`（整段覆盖）/ `load_auto_memories`；原 `add` 保留给 auto |
| `userconfig/project_http.py`（新建） | `build_project_router`：POST/GET projects |
| `userconfig/project_memory_http.py` | + `PUT/GET/DELETE project-instructions` 路由 |
| `main.py` | 组装 ProjectStore + build_project_router；注入 |
| `agentrun/configuration.py` | 第 6.5 步改为「指令 + 自动记忆」双段注入 |
| `web-v1/app/api/projects/route.ts`（新建） | BFF：POST/GET |
| `web-v1/app/api/project-instructions/route.ts`（新建） | BFF：PUT/GET/DELETE |
| `web-v1/components/settings/project-settings.tsx`（重写） | 项目选择器 + 指令编辑区 |
| `web-v1/components/settings/settings-form.tsx` | 引用新组件 |
| `web-v1/components/chat/chat-page.tsx` | 顶部项目名显示（可选） |

**删除**：`app/api/project-memories/` 的旧 BFF 保留（自动记忆用），`project-memory.tsx` 删除或废弃。

---

## 六、验收标准

1. 设置页：新建项目「我的 Python 后端项目」→ 出现在下拉 → 写指令 → 保存 → 刷新后回显
2. 聊天页：切到该项目 → 发消息「我们项目用什么技术栈？」→ Agent 回答引用了指令内容
3. 同用户建两个项目 → 指令互不串
4. 不同用户建同名项目 → 互不干扰（UNIQUE 是 `(user_id, name)`）
5. 指令保存是覆盖式（改一处，旧内容不残留）
6. v1 老库迁移后 `kind='user'` 数据正常注入，无报错
7. pytest + py_compile + ruff + tsc 全绿

---

## 七、Phase B 预留（本期不做，只留接口）

- 会话结束钩子（`service.py` `_run` finally）→ 调 LLM 总结对话 → `kind='auto'` 写入
- 自动记忆只记「新事实/偏好」，不记 PR 号/SHA/日志（对齐 wuu「不存清单」）
- 注入时指令优先、自动记忆补充，前端可见可删

---

## 八、面试话术

> 项目记忆第一版做了「项目 ID + 逐条记忆」，但产品视角有问题——把数据库概念暴露给了用户。对齐 Codex/Claude Code 改成双层：① 项目实体化，用户只命名（如「我的 Python 后端项目」），ID 系统生成；② 静态指令文档化，一个编辑区写完整份 AGENTS.md 等价物，覆盖式保存；③ 预留自动记忆层（会话结束 AI 总结，kind 区分 user/auto，前端可见可删）。用户心智从「操作数据库」变成「写下规则」，Agent 越来越懂项目。
