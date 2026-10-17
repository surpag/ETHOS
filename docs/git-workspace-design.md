# Git 工作区设计：拉取仓库到工作区 + 推送（方案待拍板）

> ⚠️ **2026-10-12 已被取代**：项目定位调整为「云端多租户代码 Agent」后，本文 §三 / §四（"git 在后端执行"的选型与凭据模型）已不适用——
> git 执行位置改为 **③ git runner 沙箱**，凭据策略按威胁模型重做。**请以 `docs/cloud-multitenant-migration.md` 为准**；
> 本文保留作为「问题空间枚举 / §4.1 能力面 / §5 边界收敛」的历史参考（这几部分仍然有效）。

> 2026-09-11 起草。触发：用户反馈「目前不能通过 git 推送项目」，并要求新增「通过 git 拉取仓库到工作区」。
> 结论先行：**git 操作放在后端（可信侧）代执行，凭据永不下沉到沙箱**。Agent 通过新工具调用，而不是在沙箱里跑 `git push`。

---

## 一、问题空间枚举

**① 谁发起 git 操作**
- Agent（自主拉取/推送）
- 用户（在 UI 上点「从 Git 导入」「推送」）

**② 操作类型**
- 只读：`clone` / `fetch` / `status` / `log` / `diff`
- 写：`commit` / `push` / `pull`（改远端或本地历史）

**③ 凭据放哪**
- 沙箱内（env / credential helper / remote URL 带 token）
- 后端进程内（每次操作临时注入）
- 用户每次粘贴（不存储）

**④ 工作区形态**（git 必须适配三种）
- 绑定 `local_root`（= 用户本地目录，方案 B）
- 未绑定的云端副本（`workspace-volumes/{id}`）
- E2B Volume 挂载

**⑤ 仓库现状**
- 已是 git 仓库（有 `.git`，可能落后于远端）
- 不是仓库（只有文件，如 `D:\agent\Data`）→ 需要 `init` + 关联远端，或直接 clone
- 空工作区 → 直接 clone

**⑥ 失败与冲突**
- 凭据无效 / 过期 / 权限不足
- 远端比本地新（push 被拒 non-fast-forward）
- 本地有未提交改动（push/pull 语义冲突）
- 网络不通 / 出口白名单拦了 git 域名
- 大仓库 / LFS
- **多用户并发**：不同用户操作各自仓库（天然隔离）；同一用户两个会话操作同一仓库（受工作区互斥租约约束）

---

## 二、关键事实（已核实，设计前提）

| # | 事实 | 来源 |
|---|---|---|
| 1 | **上传时 `.git` 被过滤**：`SKIP_DIR_SEGMENTS = {node_modules, .git, build, dist}` → 工作区快照里**没有仓库历史** | `web-v1/lib/sandbox/files.ts:20` |
| 2 | **后端没有任何 git 能力**（无 subprocess/git 调用） | 全库 grep |
| 3 | E2B 模板未显式安装 git（`from_image("e2bdev/base")` + 建目录 + 拷 skills）；沙箱内 git 是否可用**未验证** | `e2b-template/edith-sandbox-python/template.py` |
| 4 | `D:\agent\Data` 本身**不是 git 仓库**（无 `.git`） | 实测 |
| 5 | 凭据进入沙箱 = 暴露面极大：`run_command` 不受路径白名单约束，模型可读 env / `~/.netrc` / `.git/config` | 2026-09-11 边界评审（§10 of local-sandbox-sync-design） |
| 6 | 后端已有凭据混淆机制（`secret.py` XOR+Base64，`enc:v1:` 前缀），可承载 user 级 token | 既有实现 |
| 7 | E2B 出口白名单：域名过滤只对 80/443 生效、每沙箱 ≤10 域；`git push` 走 HTTPS 443 可过，但官方定位是"路由控制非安全边界" | 2026-09-11 核实 |
| 8 | 工作区互斥租约已上线（同一 workspace 同一时刻只允许一个会话） | `agentrun/workspace_lanes.py` |

**「不能推送」的因果链**：工作区里没有 `.git`（事实 1）→ 沙箱里没有凭据（零信任设计，本来就不该有）→ 后端没有 git 能力（事实 2）→ 所以 Agent 无路可走。

---

## 三、方案对比：git 在哪跑

### 方案 A：沙箱内 git（Agent 直接 `git push`）
- 需要模板装 git + 凭据进沙箱 + 出口放行
- ❌ **凭据进 VM**：模型可通过 `env` / `cat ~/.netrc` / 读 `.git/config` 拿到 token 并外泄（`run_command` 无路径约束，SSRF/外泄无拦）
- ❌ E2B 的 per-host header transform（出口代理注入，token 不过沙箱）理论可行，但 beta、每沙箱 1 rule、git 需多域名（`github.com`/`codeload`/`api`），脆弱
- ❌ `.git` 被同步排除，还得改过滤器，把仓库历史也暴露进沙箱
- ✅ 唯一优点：Agent 原生使用 git，语义自然

### 方案 B：后端代执行 git（★推荐）
- 后端是可信侧：凭据只在后端进程内、每次操作临时注入，**绝不写入 `.git/config` 或落盘**
- `clone` → 落到工作区（绑定 local_root 则直接是用户本地目录）→ 既有 B3 自动把文件带进沙箱，**零新增同步逻辑**
- `push` → 后端在工作区目录 commit + push；沙箱内的改动靠既有 B2 先同步回来
- ✅ 凭据按 `user_id` 隔离，天然支持多用户云端形态
- ✅ 可审计（每次操作落日志）、可限流、可做 SSRF 校验
- ❌ 后端需装 git（部署依赖）；大仓库 clone 占后端带宽

### 方案 C：混合（B + wuu 式能力收敛）
- 后端负责所有**需要凭据**的操作（clone/push/pull）
- Agent 在沙箱内只做**只读** git 查询——但沙箱里没有 `.git`，所以只读查询也走后端
- 即：**所有 git 能力都是新工具，沙箱内不跑 git**；`run_command` 的 tool guide 明确说明"git 操作请用 git 工具"

**推荐 B（实现上就是 C）**：凭据侧与执行侧都在后端，边界干净。

---

## 四、推荐方案：后端 Git 服务（credential brokering）

### 4.1 能力面（新增 `internal/gitops/` 模块 + 工具/HTTP 双入口）

| 工具/接口 | 说明 | 凭据 |
|---|---|---|
| `git_clone(url, branch?, target_dir?)` | 拉取仓库到工作区（空工作区直接 clone；非空工作区 clone 到子目录或拒绝） | 需要 |
| `git_status()` / `git_log(n)` / `git_diff()` | 只读查询（对 `.git` 所在目录执行） | 不需要 |
| `git_commit(message)` | 提交当前改动（**必须显式 message**，对齐 wuu 的收敛） | 不需要 |
| `git_push(branch?)` | push 前自动 `pull --rebase`，失败 → 报冲突交用户 | 需要 |
| `git_pull(branch?)` | 同上 | 需要 |
| HTTP：`POST /internal/workspaces/{id}/git/clone` 等 | UI 入口（「从 Git 导入」「推送」按钮） | 同上 |

**边界收敛（抄 wuu）**：不支持 `rebase -i` / `force push` / `reset --hard` / `clean` / 分支增删——破坏性强或难回滚，一律不做。

### 4.2 凭据与安全（关键设计，几条红线）

1. **token 只存后端**：user 级 GitHub PAT，复用 `secret.py` 的 `enc:v1:` 混淆；云端部署换真正的 secret store。按 `user_id` 隔离。
2. **token 绝不落盘到工作区**：
   - ❌ 禁止 `clone https://token@github.com/...`（会写进 `.git/config` 的 `remote.origin.url`，而本地目录可被 Agent 直读 → 泄漏）
   - ✅ `remote.origin.url` 保持干净 URL；凭据通过每次操作的 `-c http.extraHeader="Authorization: ..."` 或临时 credential helper 注入
3. **git 身份**：commit 需要 `user.name/email`，按 user 配置（设置页填，或从 PAT 关联账号取）。
4. **SSRF 校验**：clone URL 必须校验（拒绝内网/回环/链路本地地址）——`userconfig/README.md:180` 已有同类实现可直接复用。
5. **最小权限**：PAT 只给 `repo` scope；文档提示用户用 fine-grained token。
6. **出口白名单联动**：若某 workspace 配置了 `allowedDomains`，clone/push 需要的域名（`github.com` 等）必须在白名单内，否则明确报错（而不是静默失败——E2B 被拦连接会"假成功"）。
7. **审计**：每次 git 操作落日志（user/workspace/操作/远端/结果），**不记 token**。
8. **租约**：`clone` 会大改工作区，纳入工作区互斥租约（clone/push/pull 前必须持有租约）。

### 4.3 与同步机制（B1–B3）的关系

```
git clone（后端）
   └→ 写入 local_root / workspace-volumes/{id}
        └→ B3（本地 → 沙箱）把工作文件带进沙箱      ← 既有机制，零改动
             （.git 被 DEFAULT_IGNORES 排除，不进沙箱 ✅）

Agent 改文件（沙箱内）
   └→ B2（沙箱 → 本地）先同步回工作区
        └→ git_commit / git_push（后端，在本地工作区上执行）  ← 既有机制，零改动
```

**要点**：`.git` 在本地工作区、不在沙箱 —— 这是有意的。好处是凭据与历史都不暴露给模型；代价是 Agent 在沙箱里 `run_command("git status")` 会失败 → 需要在 `run_command` 的 guide 里明确"git 操作请用 git 工具"（与"不受路径白名单约束"同一段）。

---

## 五、分阶段实施

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **G1** | `internal/gitops/` 骨架 + 凭据存储（user 级 PAT，`secret.py` 混淆）+ `git_clone` + SSRF 校验 + `POST .../git/clone` | 后端装 git |
| **G2** | `git_status/log/diff` 只读工具 + tool guide 更新 | G1 |
| **G3** | `git_commit/push/pull`（push 前自动 pull --rebase，冲突报错）+ 审计 | G1 |
| **G4** | UI：设置页「连接 Git 账号」（PAT + name/email）+ 工作区「从 Git 导入」+「推送」按钮 + 冲突提示 | G1–G3 |
| **G5**（可选） | `.git` 进沙箱的只读快照（让 Agent 能看 log/diff）——**默认不做**，凭据面扩大 | — |

---

## 六、决策点（待拍板）

| # | 决策点 | 选项 | 倾向 |
|---|---|---|---|
| 1 | git 在哪跑 | A 沙箱内 / **B 后端代执行** | **B**（凭据不出后端） |
| 2 | 凭据形态 | a 后端存 PAT / b OAuth App / c 用户每次粘贴 | **a**（体验最好，云端多用户必需）；c 可作为 a 的降级 |
| 3 | clone 到哪 | a 空工作区 / b 已绑定工作区的子目录 / c 都支持 | **c**，但非空且非 git 的工作区默认拒绝，避免覆盖用户文件 |
| 4 | Agent 能否自主 push | a 可以（工具直接暴露）/ b 仅用户点按钮 / c Agent 可 commit、push 需确认 | **c**（对齐"推送是把改动交给外部世界"的边界） |
| 5 | 平台范围 | 只 GitHub / 任意 git URL | 先 **GitHub**（凭据语义清晰），架构上按"任意 git over HTTPS"设计 |
