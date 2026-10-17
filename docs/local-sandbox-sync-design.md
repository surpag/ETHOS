# 本地目录 ↔ 沙箱 双向同步设计（方案 B）

> ⚠️ **2026-10-12 定位调整**：项目定位转为「云端多租户代码 Agent」，本地目录绑定在云端语义不成立。
> 该线**不再作为主线**，收敛为可选的 `VolumeBackend` 实现（保留代码与测试，默认关闭）。**后续以 `docs/cloud-multitenant-migration.md` 为准**。
> 本文保留作为：① B1–B3 已实施代码的设计依据 ② §十「写入/沙箱边界（对照 wuu）」中的安全边界结论（D1 授权校验 / 出口白名单 / 互斥租约 / 审计 —— 这些在云端**仍然有效且更重要**）③ 第 1.5 节"失败枚举"与 §4.2 状态表（云端 git 场景可复用）。

> 状态：设计稿，待评审。**未实施任何代码改动。**
> 目标：让用户的本地项目目录（如 `D:\agent\Data`）与 E2B 沙箱保持双向一致，
> 消除当前"双副本分裂"导致的"Agent 产出回不到本地""本地改动沙箱看不到"。
> 本文遵循「先枚举问题空间，再设计机制；诊断与恢复依据解耦；通用性第一」的原则。

---

## 一、问题空间枚举

### 1.1 现状（3 层，2 处断裂）

```
D:\agent\Data  --[上传:一次性快照]-->  workspace-volumes/{id}/  --[连接时同步]-->  E2B 沙箱
   ↑ 用户看的"本地"                     ↑ write_file 双写目标                ↑ Agent 工作区
      （不在同步链路里）                    （用户看不到，且不可下载）
```

- **断裂点 1**：上传是**一次性快照**，之后本地改动不再进入系统。
- **断裂点 2**：Agent 产出写入 volume 副本（`workspace-volumes/{id}/`），且 `workspace/` 下的文件**不可下载**（`sandbox/http.py:129` 仅允许 `artifacts/`）。
- **盲区**：双写仅覆盖 `sandbox_write_file`；`sandbox_run_command` 内写文件（heredoc / 脚本落盘）**完全丢失**。

### 1.2 变化来源枚举

| # | 来源 | 检测方式 | 同步方向 |
|---|---|---|---|
| 1 | 本地新增文件 | mtime/size 扫描（或 watchdog 脏列表）| 本地 → 沙箱 |
| 2 | 本地修改文件 | mtime 变化 + hash 复核 | 本地 → 沙箱 |
| 3 | 本地删除文件 | 基线比对（存在性）| 本地 → 沙箱（软删，见 §5.3）|
| 4 | 本地重命名 | 删除 + 新增 | 本地 → 沙箱 |
| 5 | 沙箱 `write_file` | 工具结果直接落盘（已有双写）| 沙箱 → 本地 |
| 6 | 沙箱 `run_command` 写文件 | **工具调用后扫描沙箱目录比对基线** | 沙箱 → 本地 |
| 7 | 沙箱删除/重命名（Agent 用命令）| 基线比对 | 沙箱 → 本地（软删）|

### 1.3 时机枚举

| 时机 | 用途 |
|---|---|
| 工具调用**前** | 本地 → 沙箱（让 Agent 看到最新本地代码）|
| 工具调用**后** | 沙箱 → 本地（把 Agent 产出拉回来，覆盖 run_command 盲区）|
| **用户发新消息时**（`chat()` 入口）| 本地 → 沙箱（覆盖"两轮对话之间用户改了代码"的场景）★用户补充 |
| 用户手动触发 | 全量重扫（兜底）|
| 沙箱重建时 | **强制全量基线重建**（见 §3.3）|

### 1.4 冲突枚举

| 场景 | 处理 |
|---|---|
| 同一文件两边都改 | 默认**报错暂停**（`status='conflict'`），UI 提示用户决策 ★用户补充 |
| 一边删一边改 | 视为冲突，暂停 |
| 重命名撞已存在路径 | 按冲突处理 |

### 1.5 失败枚举（通用性验证）

| 失败 | 处理 |
|---|---|
| 网络抖动（E2B 远程）| 指数退避重试；失败文件标 `dirty` 留待下次 |
| 沙箱超时/重启 | `sandbox_id` 变化 → **全量重新基线** |
| 进程崩溃 | 状态表 WAL 已持久化 → 下次增量恢复 |
| 磁盘满 / 权限 | 记日志 + 标 `dirty`，不阻塞 run |
| 大文件/二进制 | `.edithignore` + 单文件上限，跳过并记日志 |

---

## 二、关键事实（已核实，设计前提）

1. **E2B 沙箱是远程的**（在 E2B 服务器上）。即使后端本地运行，本地 ↔ 沙箱仍走网络 → 必须处理抖动、超时、限速。★用户补充 1
2. **沙箱按 session 隔离**：`user_sandboxes(user_id, session_id, sandbox_id)`。一个 session 一个沙箱。
3. **沙箱内按 workspace 分目录**：`workspace/{workspace_id}/...`。
4. **workspace 与 sandbox 是多对多**：同一 workspace 可在多个 session 中使用（对应多个沙箱）；同一沙箱内可挂多个 workspace 目录。
5. **后端与用户本地同机**（Local 部署）→ 后端可直接读写 `local_root`，**不需要** File System Access API。
6. workspace metadata 只有 `folderName` + 相对路径，**没有**源目录绝对路径。

> ⚠️ 由事实 2/3/4 推出（用户提的坑）：**同步状态的键必须包含 `sandbox_id`**，不能只用 `(workspace_id, path)`——否则同一 workspace 在两个沙箱里的同步状态会互相污染。

---

## 三、架构设计

### 3.1 目标形态（2 层）

```
D:\agent\Data  ◀══[双向增量同步]══▶  E2B 沙箱（workspace/{id}/）
   ↑ 用户看的"本地" = local_root        ↑ Agent 工作区
```

关键：**让 workspace 的存储根 `local_root` 直接指向用户本地目录**，消除 `workspace-volumes/{id}/` 这一中间副本。

### 3.2 全量基线同步 vs 增量同步（分离设计）★用户补充 2

两个场景必须走**全量**：

| 场景 | 触发 |
|---|---|
| **首次绑定** | `local_root` 首次设置 → 本地全部文件上传沙箱，建立基线 |
| **沙箱重建** | `sandbox_id` 与状态表不匹配 → 清空该 (workspace, sandbox) 状态 → 全量重建 |

状态表用 `baseline_synced` 标记区分"已建立基线"（增量）与"未建立"（全量）。

### 3.3 两级检测 + 脏文件列表（性能）★用户补充 3

避免"每次工具调用前全目录扫描"：

```
第一级：比 mtime + size（元数据，不读文件内容）—— 快
第二级：仅当 mtime/size 变化才算 hash（读盘）—— 慢但精确
可选：本地 watchdog 后台监听，维护"脏文件列表"，工具调用前只处理脏文件
```

### 3.4 大文件与忽略规则 ★用户补充 4

- `.edithignore`（`.gitignore` 语法）：排除 `node_modules/`、`*.mp4`、`*.zip`、`.venv/` 等
- 单文件上限（默认 10MB）：超过则跳过 + 记日志（标 `skipped`）
- 二进制文件：仅比 hash，不做文本处理

---

## 四、数据模型

### 4.1 `workspaces` 表新增

```sql
ALTER TABLE workspaces ADD COLUMN local_root TEXT;      -- 本地绝对路径（绑定后非空）
ALTER TABLE workspaces ADD COLUMN local_root_bound_at TEXT;
```

### 4.2 `workspace_sync_state`（含用户补充的字段）

```sql
CREATE TABLE IF NOT EXISTS workspace_sync_state (
    workspace_id  TEXT NOT NULL,
    sandbox_id    TEXT NOT NULL,        -- ★关联沙箱；重建时该值不匹配 → 触发全量
    path          TEXT NOT NULL,        -- 相对 local_root 的路径
    source        TEXT,                 -- 'local' | 'sandbox'：最后修改来源（冲突判定）
    mtime         REAL,
    size          INTEGER,
    hash          TEXT,
    synced_at     REAL,
    status        TEXT DEFAULT 'synced',-- 'synced'|'dirty'|'conflict'|'deleted'|'skipped'|'conflict_pending'
    PRIMARY KEY (workspace_id, sandbox_id, path)
);
CREATE INDEX idx_sync_ws_status ON workspace_sync_state (workspace_id, sandbox_id, status);
```

> 与用户草案的差异：**主键加入 `sandbox_id`**（因沙箱是 session 级、workspace 可多沙箱）。

### 4.3 基线标记

```sql
CREATE TABLE IF NOT EXISTS workspace_sync_baseline (
    workspace_id TEXT NOT NULL,
    sandbox_id   TEXT NOT NULL,
    baseline_synced INTEGER NOT NULL DEFAULT 0,   -- 0=需全量，1=已建立
    baseline_at  TEXT,
    PRIMARY KEY (workspace_id, sandbox_id)
);
```

---

## 五、核心机制

### 5.1 workspace 绑定 local_root

- 创建 workspace 时输入本地绝对路径（决策点 1 选 A）
- `LocalVolumeBackend` 读到该 workspace 有 `local_root` 时，**读写直接落在 local_root**
- 路径安全：白名单根 + 拒绝 `..` 穿越（沿用现有 `resolve_workspace_path` 的安全策略）

### 5.2 双向增量同步算法（伪代码）

```
async def sync(workspace_id, sandbox_id, direction):
    if not baseline_synced(workspace_id, sandbox_id):
        await full_baseline(workspace_id, sandbox_id)   # 全量
        return
    if direction in ("local->sandbox", "both"):
        for f in scan_local(workspace_id):               # 两级检测 + 脏列表
            st = state(workspace_id, sandbox_id, f.path)
            if f.mtime != st.mtime or f.size != st.size:
                if hash(f) != st.hash:
                    if st.source == "sandbox" and f.mtime < st.mtime:
                        conflict(f); continue            # 冲突：暂停
                    await upload(f); mark_state(f, source="local")
    if direction in ("sandbox->local", "both"):
        for f in scan_sandbox(workspace_id):             # 含 run_command 写的文件
            st = state(workspace_id, sandbox_id, f.path)
            if f.hash != st.hash:
                await download(f); mark_state(f, source="sandbox")  # 落 local_root
    prune_deleted(workspace_id, sandbox_id)              # 软删传播
```

### 5.3 删除/重命名传播（软删）★用户补充 4

- 采用**传播 + 软删**（替代原"不传播"）
- 本地删 → 沙箱对应文件标 `status='deleted'`，延迟到"确认无引用"再物理删（默认保留 N 天）
- 定期清理任务处理 `deleted` 状态

### 5.4 冲突处理（默认暂停）★用户补充 4

- 检测到 `source='sandbox'` 且本地也改了 → `status='conflict'`，**不自动覆盖**
- 前端提示"文件 X 有冲突，请选择保留哪边"，用户决策后按选择同步
- 可配置：`local_wins` / `sandbox_wins` / `ask`（默认 `ask`）

### 5.5 网络容错 ★用户补充 1

- E2B 调用统一走重试包装（指数退避，区分可重试/不可重试）
- 单文件同步失败 → 标 `dirty`，不阻塞 run，下个时机重试
- 上传/下载限速（可配），避免大目录打满带宽

### 5.6 同步时机接入点

| 接入点 | 方向 | 位置 |
|---|---|---|
| `chat()` 入口 | local → sandbox | `agentrun/execution.py` run 开始前 ★用户补充 |
| 工具调用前 | local → sandbox | `SandboxToolSet.execute` 前 |
| 工具调用后 | sandbox → local | `SandboxToolSet.execute` 后（替换现有双写，统一为扫描比对）|
| 沙箱连接时（`workspace()`）| both（仅增量）| `sandbox/service.py` |
| 沙箱重建时 | 全量 | 同上 + baseline 重置 |

---

## 六、决策点（已拍板）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 本地目录指定 | **A** 前端表单输入绝对路径 + **后端白名单校验** |
| 2 | 同步时机 | 工具前后 + **用户发消息时**（`chat()` 入口）|
| 3 | 删除/重命名 | **B** 传播 + 软删（`status='deleted'`，延迟清理）|
| 4 | 冲突策略 | 默认**报错暂停**（`status='conflict'`），由 UI 让用户决策 |

### 6.1 附加决策：一个 workspace 只允许一个 session（★新增，实测触发）

> **2026-09-11 修正**：本节原方案是「严格一一对应」。实施时改为 **互斥租约**（同一 workspace 同一**时刻**只允许一个会话使用），见 §10.4-④——
> 严格绑定会让 `a281c611...`（Data 项目）已用的 **9 个历史会话**中的 8 个直接被拒，体验代价过大；
> 而正确性要求防的是「两个会话**并发**镜像同一个本地目录」，互斥租约已完整覆盖，且不破坏历史数据。
> `session_id` 字段已落库（创建时可指定、旧数据惰性绑定），前端"切 workspace = 切 session"仍建议后续做。

**现状**：`workspaces` 表**无** session 绑定字段；实测 `a281c611...`（Data 项目）被 **9 个 session** 使用过。`chat_messages.workspace_id` 只记录消息归属，workspace 与 session 是**多对多**。

**目标**：workspace ↔ session ↔ sandbox 一一对应，直接简化同步模型（状态表可退化为 `(workspace_id, path)`）。

**改动清单**：
1. `workspaces` 表加 `session_id TEXT`（绑定会话）+ 幂等迁移 —— **已做**
2. 创建 workspace 时一并创建/绑定 session（`workspace/http.py create_workspace`）—— **已做**（请求体带 `sessionId`）
3. 发消息时校验：同一 workspace 不允许被**其他会话并发**使用（`agentrun/service.py` 入口，`workspace_busy`）—— **已做**（互斥租约）
4. 前端：workspace 切换器**不再新建 session**，切 workspace = 切到它绑定的 session（`chat-page.tsx`、`workspace-switcher.tsx`）—— **待做**
5. **历史数据兼容**：旧 workspace（`session_id` 为空）→ 首次访问时**惰性绑定**当前 session，不破坏旧数据 —— **已做**（`store.bind_session`）

---

## 七、分阶段实施 + UI 策略（已拍板）

| 阶段 | 后端内容 | UI 需求 | 理由 |
|---|---|---|---|
| **B1a** | workspace 绑定 `local_root` + **只读直连** | **无 UI**，仅日志验证 | 先验证"消除副本"可行性，UI 反而干扰 |
| **B1b** | **读写直连**（写落 local_root）| **无 UI**，日志 | 同上 |
| **B2** | 沙箱 → 本地增量同步（含 run_command 盲区）| **无 UI**，日志记录同步结果 | 自动过程，用户无需干预 |
| **B3** | 本地 → 沙箱增量同步 | **最小 UI**：一个"同步状态"指示器（同步中/已完成/失败）| 用户需知道本地改动是否已生效 |
| **B4** | 冲突 + 软删 + 崩溃恢复 + 沙箱重建全量 | **必须有冲突 UI**：文件列表 + "保留本地/保留沙箱"按钮 | 冲突不处理 → Agent 用错版本，机制作废 |
| **B5** | `.edithignore` + 大文件上限 + 限速 | 无 UI | — |

**横切决策**：
- **手动同步按钮**：**做**（前端按钮 + 后端接口，兜底全量触发）——成本低、必要
- **其他 UI**（进度条、批量操作、同步历史）：**暂时不做**

---

## 八、风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| **多租户** | `local_root` 绑定 = 一台机器一个用户；云端多租户不适用 | 云端改 File System Access API / 客户端 |
| **越权读写** | 后端可读写任意本地路径 | 路径白名单（决策 1）+ 显式绑定确认 + 拒绝 `..` |
| **E2B 文件系统差异** | 符号链接/权限位可能不同步 | 只同步普通文件，跳过特殊类型 |
| **首次全量慢** | 大目录第一次同步耗时 | 分块 + 限速 + 进度提示 |
| **watchdog 依赖** | 后台监听需常驻进程 | 设为可选优化，默认用扫描 |
| **冲突误判** | mtime 在编辑器保存时不可靠 | 默认 `ask` 暂停，不自动覆盖 |

---

## 九、已拍板 / 待确认

**已拍板**：
1. workspace ↔ session：**互斥租约**（同一 workspace 同一时刻只允许一个会话使用；历史会话顺序可用，不破坏数据）——2026-09-11 由"严格一一对应"修正为"互斥租约"，理由见 §10.4-④
2. `local_root` 入口：**前端表单绝对路径 + 后端白名单校验**
3. UI 策略：B1/B2 无 UI · B3 最小指示器 · B4 冲突 UI（硬需求）· 手动同步按钮做；其余暂时不做
4. 沙箱出口白名单：**默认放通 + 每 workspace 可收紧（metadata.allowedDomains）+ 环境变量兜底**（2026-09-11 拍板）

**待确认（B4 前需明确）**：
- 冲突 UI 展示位置（会话内联 vs 独立面板）
- ~~白名单配置形式~~ → **已定**：`metadata.allowedDomains`（≤10 个，E2B 硬限）+ `EDITH_SANDBOX_ALLOWED_DOMAINS` 兜底；设置入口 = 设置页「沙箱出口白名单」
- 软删清理周期（默认保留 N 天）

---

## 十、写入 / 沙箱边界（对照 wuu，2026-09-11 新增）

> 背景：参照另一项目 wuu 的写入/沙箱架构做了一次对照评审，据此核查并补齐了 EDITH 的边界能力。
> wuu = **本机执行** + 路径边界（`ResolvePath`）+ macOS Seatbelt + 可选 git worktree 隔离，**没有镜像同步层**（写入即真实文件）。
> EDITH = **远程 E2B VM 执行** + **镜像同步**（双写 / B2 / B3）。

### 10.1 结构性差异（决定哪些能抄、哪些不能）

| wuu 的机制 | EDITH 是否有同一问题 | 结论 |
|---|---|---|
| `ResolvePath` 进程内路径校验 | 有对应物：`sandbox/workspace.py::workspace_path()` | 已有，但曾是"格式校验"，缺"授权校验"（→ §10.4-① 补齐） |
| macOS Seatbelt（子进程 OS 沙箱） | 不适用：工具跑在远程 VM，宿主机不可能被写 | 但 **VM 内**路径/网络约束曾是空白（→ §10.4-②③） |
| git worktree「隔离 → 显式合并」 | 不同范式：EDITH 是「持续镜像」 | 复杂度在**一致性**，不在隔离；`Review → Preview(--check) → Apply` 三段式可作为 B4 冲突 UI 的模板 |
| 敏感路径（`.git`/`.env`/私钥） | 有，且曾完全没做 | 部分抄（工具层已收口；`run_command` 见 §10.5） |
| 同文件写入串行化 | 部分：单 run 内串行，跨会话无锁 | → §10.4-④ 工作区互斥租约 |
| 装不上沙箱就报错、不静默降级 | 有类似精神（E2B 不可用 → `tool_failure` + 熔断） | 已对齐 |
| 错误面向模型（ModelNextAction） | 已有 `suggestion` 字段 | 已对齐 |

**一句话**：wuu 缺"同步"，我们缺"边界"。可抄的是它的边界思维，不是它的 worktree。

### 10.2 问题空间枚举

- **写入者**：Agent 文件工具 · Agent `run_command`（heredoc/`sed -i`/脚本）· 用户本地编辑 · 用户上传 · 系统自身（双写/同步/恢复）
- **目标路径**：本工作区 · **其他工作区** · 共享目录（`uploads/ work/ artifacts/ skills/`）· 系统路径（`~/.ssh`、`~/.netrc`）· **网络出口**
- **落盘模式**：未绑定副本 · 绑定 local_root（prefix 空/非空）· E2B Volume 挂载
- **失败**：E2B 超时/不可用 · 同步失败 · 本地盘满/权限 · 路径非法 · 并发写 · 进程崩溃
- **并发**：同 run 多工具 · 同 workspace 多会话 · 用户同时改本地

### 10.3 现状核查（2026-09-11，均有代码证据）

**已具备**：`workspace_path()` 拒绝绝对路径 / `..` / 白名单外顶层目录；本地直读额外拒绝 `..` 与绝对子段；错误语义分层 + `suggestion`；熔断 + 轮次上限 + token 预算；E2B VM 硬隔离（保护宿主机）。

**评审前缺口（已按 §10.4 处理）**：
1. **跨工作区越权**：`workspace_path()` 只校验"第二段存在"，不校验等于当前会话的工作区 → `sandbox_read/write/delete_file("workspace/{other_id}/...")` 可通过；E2B Volume 模式下所有工作区都挂进同一沙箱，绑定 local_root 后还会直写对方本地目录。
2. `run_command` 零约束（`cwd=work/` 但可 `cd /`，可读 `~/.ssh/*`）。
3. 无网络出口控制（`AsyncSandbox.create` 未传 `allow_internet_access`，默认全放通）。
4. 无敏感路径概念（`.env`/`.git` 可被读写/破坏）。
5. 无 workspace 级并发控制 → 双写/B2/B3 互相覆盖。
6. 同步失败静默 → 本地与沙箱不一致无人知晓。

### 10.4 已实施（四项决策，2026-09-11）

| # | 决策 | 实现 | 位置 |
|---|---|---|---|
| ① | **禁止跨工作区**（一个会话只能碰自己的工作区） | `workspace_path()` 新增 `current_workspace_id`，`workspace/{other_id}` → `PermissionError`（格式校验 → **授权校验**）；工具层**单一卡点** `_scope_violation()` 在参数补全后统一校验，越权返回 `command_error + retryable + suggestion`（让模型改用相对路径自救） | `sandbox/workspace.py` · `sandbox/tools.py` |
| ② | **`run_command` 事后审计 + 显式告知**（模板级 OS 约束单独立项） | 工具描述写明不受路径白名单限制；结果带 `sandboxScope: "unrestricted"`；`_MUTATING_TOOLS` 统一打 `sandbox_mutation` 审计日志（`_audit_digest` 跳过文件正文） | `sandbox/tools.py` |
| ③ | **出口白名单**：默认放通 + 每 workspace 可收紧 + 环境变量兜底 | 优先级 `metadata.allowedDomains` → `EDITH_SANDBOX_ALLOWED_DOMAINS` → 空（放通）；创建时传 `network={allow_out, deny_out:[ALL_TRAFFIC]}`，已有沙箱在**连接时**经 `update_network` 补发（memo 防重复）；超 10 个截断告警；下发失败显式告警 | `sandbox/service.py` · `workspace/http.py` · 设置页 |
| ④ | **工作区互斥租约**（只禁并发，不破坏历史会话） | 新增 `WorkspaceLanes`（同会话可重入；释放他人租约是 no-op）；会话锁之后 acquire，被其他会话占用 → `workspace_busy`；3 处 release 同步释放 | `agentrun/workspace_lanes.py` · `agentrun/service.py` |

**反模式防护**：`tests/test_stage10.py::test_path_fields_covers_all_file_tools` 断言 `_FILE_TOOLS ⊆ _PATH_FIELDS`——新增文件工具若忘记登记授权校验，测试直接失败而非静默放行（呼应"不要特例补丁"）。

### 10.5 E2B 出口白名单的能力与局限（官方文档 + SDK e2b 2.39.0 核实）

| 事实 | 影响 |
|---|---|
| `allow_out` 支持 hostname / IP / CIDR / `*.example.com` | ✅ 可做域名白名单 |
| 必须同时 `deny_out=[all_traffic]`，否则不是白名单 | 实现已按此写 |
| **每沙箱最多 10 个域名**，超限 400、不可提额 | 所以选 per-workspace，全局一刀切必不够用 |
| 域名过滤只对 80(Host)/443(SNI) 生效；QUIC 不支持 | 非 HTTP 协议管不住 |
| 官方自述是 "routing control，不是严格安全边界"（共享 CDN/S3 端点可能连带可达） | 定位要诚实 |
| **被拦的连接从沙箱内看可能"成功"**（连接先接受再判定） | 验证必须看应用层响应 |
| `update_network()` 运行时整体替换（省略字段被清空） | 已有沙箱也能收紧 |

注意：MCP 连接是**后端**发起的，不受沙箱出口策略影响；收紧出口的主要功能损失是 `pip install` / `npm install` / 任意 `curl`，因此白名单要放行对应源。

### 10.6 遗留与边界

1. per-workspace 白名单目前经创建时 `metadata.allowedDomains`、设置页、或环境变量设置；**"给已有工作区改白名单"的接口与入口已补**（`PUT /internal/workspaces/{id}/allowed-domains` + 设置页）。
2. 清空白名单恢复"全放通"需重建沙箱（或手动 `update_network({})`）——边缘场景。
3. 审计是日志级；E2B 不暴露被拦连接日志，白名单是否生效须用应用层响应验证。
4. 工作区租约是**进程内**实现（`--reload` / 单 worker 下正确）；多 worker 部署需换 DB/Redis 租约。
5. **模板级 OS 约束（bubblewrap/seccomp/低权用户）单独立项**——EDITH 拥有自己的 E2B 模板（`e2b-template/edith-sandbox-python/template.py`），这是 Linux 上等价于 Seatbelt 且可落地的路径。
6. 敏感路径（`.env`/`.git`/凭据）在**工具层**已收口；`run_command` 仍可触达，彻底解决依赖第 5 条。
7. B4 状态表（`workspace_sync_state`）仍是同步一致性的根治方案（见 §4.2）。
