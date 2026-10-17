# 云端多租户代码 Agent：定位调整与改造方案

> 2026-10-12 起草。本文是**定位调整后的唯一权威文档**，取代 `local-sandbox-sync-design.md`（本地同步线）与 `git-workspace-design.md`（旧版 Git 方案，假设"后端在工作区目录上直接跑 git"）中与新定位冲突的部分。
>
> **结论先行**：
> - 定位从「单机 + 本地目录绑定」转为「云端多租户代码 Agent」→ 本地目录线**收敛**（不删除，收敛为独立 backend 实现）。
> - 工作区文件的**唯一权威副本 = E2B Volume**，沙箱靠**可写挂载**直接读写 → **B1–B3 同步层整体移除**。
> - Git 远程管理采用 **③ git runner 沙箱**（后端控制、Agent 不可达）。
> - 凭据托管是**必须自研**的部分——wuu 的"零注入"策略恰恰建立在"单用户本地应用"前提上，云端完全不可用。

---

## 一、定位变更与失效假设

| 原假设（单机 + 本地绑定时代） | 云端多租户下的状态 |
|---|---|
| 后端与用户同机，可直连用户磁盘 | ❌ **失效** → `local_root` 绑定在语义上不成立 |
| 工作区文件「本地一份 + 沙箱一份」双副本 | ❌ **失效** → 改为「Volume 一份 + 挂载给沙箱」 |
| 需要双向镜像同步（B1–B3）来弥合双副本 | ❌ **不再需要**（见 §二-1） |
| workspace 是"用户某台机器上的目录" | ✅ 改为"租户拥有的云端存储单元" |
| 权限边界以"用户"为单位 | ✅ 升级为"租户"为单位（配额、密钥分层） |

---

## 二、关键事实（已核实）

| # | 事实 | 来源 |
|---|---|---|
| 1 | **E2B Volume 挂载可写**：`SandboxVolumeMount` 只有 `name` / `path`，**无只读开关** → 沙箱直接改 Volume → **同步层可整体移除**（"双副本分裂"的更彻底解法：只留一份） | `e2b/api/client/models/sandbox_volume_mount.py:11` |
| 2 | **后端对 Volume 只有文件级 API**（`read_file` / `write_file` / `make_dir` / `remove` / `list` / `get_info`）→ **后端无法在 Volume 上直接跑 git** | `e2b/volume/volume_async.py` |
| 3 | **工具层没有任何敏感路径保护** → Volume 可写挂载后，Agent 能自由读写/删除 `.git`（仓库元数据） | `sandbox/{tools,workspace}.py` 全库 grep 为空 |
| 4 | **SSRF 校验已有可复用实现**（拦截 localhost / loopback / private / link_local / unspecified） | `userconfig/mcp.py:110-115` |
| 5 | **Volume 命名已含租户标识**：`ws_{workspace_id[:12]}_{user_id[:8]}` → 租户隔离在命名层已具备 | `workspace/volume_backend.py:90` |
| 6 | 当前仍是单机配置：`EDITH_WORKSPACE_VOLUME_BACKEND=local`、`EDITH_WORKSPACE_LOCAL_ROOTS=D:\agent\Data` | `backend-py/.env` |
| 7 | E2B 出口白名单能力与局限见 `local-sandbox-sync-design.md §10.5`（10 域上限 / 仅 80·443 / 官方定位"路由控制非安全边界"） | 2026-09-11 核实 |

---

## 三、改造清单

### 3.1 收敛（本地目录线）—— **不删除，收敛为独立 backend 实现**

**判据（取自 wuu 的"单一权威源"原则）**：本地模式应当是**一个 `VolumeBackend` 实现**，而不是散落在 8 个文件里的 `if local` 分支。真正的复杂度来源不是这个功能，而是**逻辑穿层**。

| 层 | 当前位置 | 目标 |
|---|---|---|
| 存储实现 | `volume_backend.py` 的 `LocalVolumeBackend`（含 `_local_roots` / `_prefixes` / `bind_local_root` / `_strip_prefix` / `resolve_workspace_path` / `legacy_dir`） | **保留**，但明确其定位 = "单机 dev / 测试用 Volume 实现"；绑定与前缀逻辑内聚进该类，不外泄 |
| 绑定入口 | `workspace/http.py`（`_validate_local_root` / `_ALLOWED_LOCAL_ROOTS` / `_detect_local_root_prefix` / `POST bind-local-root`）+ `types.py`（`BindLocalRoot*`） | 保留为**可选功能**，默认关闭（`EDITH_WORKSPACE_LOCAL_ROOTS` 未配置即不可用） |
| 同步层 | `workspace/sync.py`（233 行：`sync_sandbox_to_local` / `sync_local_to_sandbox` / `merge_dir_add_only` / `scan_local_fingerprint`）+ `sandbox/service.py` 的同步方法 + `sandbox/tools.py` 的 B2/B3 调用点 | **随后端切换为 `e2b` 后不再触发**；保留代码与测试，但标记为"仅 local backend 使用" |
| 表结构 | `schema.py` / `store.py` 的 `local_root` / `local_root_prefix` | **保留列**（删除列成本高于收益，且 local backend 仍需） |
| 前端 | `workspace-switcher.tsx` bind 模式、`app/api/workspaces/route.ts`（localRoot 透传）、`app/api/workspaces/[id]/bind-local-root/route.ts` | 保留，云端部署不暴露入口 |

> **建议动作（本阶段只做这一件）**：把默认配置切到 `e2b`，让 local 线变成"要用才开"的可选 backend。已投入的 B1–B3 测试保留在 backend 层，将来加回任何新存储形态（如 S3 直存）也只是新增一个实现类。

### 3.2 改造

1. **存储后端默认切 `e2b`**：`EDITH_WORKSPACE_VOLUME_BACKEND` 默认值改为 `e2b`（代码默认值 + 部署配置）。
2. **移除同步触发**：`sandbox/service.py::_sync_workspace_files` 的 bound 分支与 `_b3_fingerprints` 随同步层一起失效；`_MUTATING_TOOLS` 集合**保留但用途仅剩审计**。
3. **出口白名单默认值重议**：当前"默认放通 + 可收紧"在多租户下太松 → 云端建议"**默认收紧 + 平台下发基线白名单**"（含 `github.com` 等 git 域名），租户可追加（受 10 域上限约束）。
4. **工作区文件生命周期**：Volume 成为唯一副本后，需要明确"租户删除工作区即销毁 Volume"（现有 `on_delete` 已做）与**容量配额**（见 §七）。

### 3.3 保留（云端更需要）

授权校验（禁跨工作区，D1）、出口域名白名单、工作区互斥租约、变更审计日志、Skills、文件上传、usage 计费、COS 图片、Volume 挂载本身。

### 3.4 新增

| 项 | 内容 | 见 |
|---|---|---|
| Git 远程管理 | clone / status / log / diff / commit / push / pull（危险操作一律不给） | §五、§六 |
| 凭据托管 | 平台侧持有租户 git 凭据、按租户隔离、短命、可撤销 | §四 |
| `git` 敏感路径保护 | Agent 沙箱内禁止读写 `.git/**` | §5.6 |
| 多租户 | 配额、平台密钥 vs 租户密钥分层 | §七 |

---

## 四、凭据策略（**必须自研**：wuu 帮不上）

### 4.1 为什么 wuu 的"零注入"不适用

wuu 是**单用户本地应用**——git 凭据天然存在于用户机器（`credentialstore` 后端是 macOS Keychain / 文件降级）。它整个凭据策略是「**零注入**」：不搬运凭据，因为凭据本来就在位。

云端多租户下这个前提**完全反转**：
- 平台必须**代表租户**持有 git 凭据（用户不可能把 PAT 放到平台控制的机器上还指望它安全，但平台又必须能推送）
- 因此**凭据托管与注入点是平台独有的、必须自研的问题**——wuu 在这个维度上给不出任何可借鉴的东西。

**但**：wuu 在"凭据已经存在的前提下如何降低爆炸半径"上有一整套手段（脱敏、命令白名单、地板保护、fail-closed），这些**全部适用**，见 §五。

### 4.2 威胁模型枚举（先枚举，后设计）

| # | 威胁 | 攻击面 |
|---|---|---|
| T1 | Agent 沙箱内读到凭据 | `env` / `cat .git/config` / `ps e` / `/proc/*/environ` / credential store |
| T2 | 凭据泄漏到日志或工具结果 | `git remote -v` 输出、`git push` 的 stderr、报错信息 |
| T3 | 凭据经 URL 形式持久化 | `clone https://token@...` 写进 `.git/config` 的 remote URL，随 Volume 长期保存 |
| T4 | 跨租户使用 | A 租户的 token 被用于 B 租户的工作区 |
| T5 | 凭据长期有效 | 一次泄漏永久可用 |
| T6 | 越范围使用 | 用租户 token 推**平台仓库**或该租户未授权的仓库 |
| T7 | 平台密钥被间接使用 | Agent 触达 `E2B_API_KEY` / COS 密钥 / 平台模型 key |

### 4.3 设计原则（P1–P8）

| # | 原则 | 对应威胁 | 实现要点 |
|---|---|---|---|
| **P1** | **凭据只在「git 执行环境」，不在「Agent 执行环境」** | T1 | 采用 **③ runner 沙箱**（§六）：两个沙箱物理隔离，Agent 侧根本没有 token，"读到"无从谈起 |
| **P2** | **凭据不落地** | T3 | remote URL 保持干净；不写 `.git/config`、不写 credential store；runner 短生命周期，凭据只存在于该 VM 内存/环境，随销毁消失 |
| **P3** | **最小权限 + 短命** | T5、T6 | 优先 **GitHub App installation token**（1h、限定仓库、可撤销）> fine-grained PAT（可限单仓 + 过期）> classic PAT（不推荐） |
| **P4** | **按租户隔离 + 绑定授权仓库集合** | T4、T6 | 凭据键 = `(user_id, repo_full_name)`；每次 git 操作前校验目标 remote ∈ 该租户授权集合 |
| **P5** | **输出全量脱敏（fail-closed）** | T2 | 所有 stdout/stderr/异常在返回 Agent 之前过脱敏器；**疑似凭据但无法确认时不返回原文**（见 §5.3） |
| **P6** | **平台密钥地板保护** | T7 | 平台密钥（E2B / COS / 平台模型 key）**任何模式**下都不可触达：不进 Agent 环境变量、不进工具结果、不进 runner 沙箱以外的环境 |
| **P7** | **审计 + 归属标记** | — | 每次操作记 `(user, workspace, repo, branch, op, commit sha, 结果)`，**不记 token**；推送的 commit 打平台标记，便于区分"AI 改的" |
| **P8** | **fail-closed** | 全部 | 凭据缺失 / 校验失败 / runner 起不来 → **明确报错**，绝不静默降级到"无鉴权尝试"或"借用平台凭据" |

**注入点选项（P1 的实现细节）**：
- **(a) 后端创建 runner 时经 `envs=` 注入**——简单、可立即验证；风险是 runner 内 `ps e` 可读 env → 靠 P3（短命 token）+ P2（用完即毁）压制
- **(b) E2B 出口代理注入**（`network.rules` transform + `iam` workload identity）——token **从不进入任何 VM**，最干净；但 feature 处于 beta，且 git 的 HTTP Basic 鉴权行为需实测
- **建议**：**先 (a) 打通，(b) 作为演进**；两者都满足 P1（Agent 沙箱无凭据）

---

## 五、Git 安全护栏（可直接借鉴 wuu 的部分）

### 5.1 三层命令白名单（**默认拒绝**）

结构照搬 wuu（其 `internal/tools/git.go`）：全局层面 → 子命令层面 → 敏感内容层面。**关键是第三层的 `else` 分支直接报错**——新命令必须显式加白名单。多租户下如果用"默认放通 + 黑名单"，漏一个就是越权。

```
第一层 全局拦截：blockedGlobalArgPrefixes = ["--config-env", "--exec-path"]
                （可让 git 加载任意可执行文件/环境变量 → 提权入口）
                shellMetacharacters = ";&|$`><()"
第二层 子命令级：policiedSubcommands → flag 级校验；allowedGitSubcommands → 通用校验
                其余 → 直接拒绝
第三层 敏感内容：validateSensitiveGitContentArgs（独立于前两层）
```

### 5.2 `blockedCommitFlags`（多租户审计生命线）

必须禁止：`--no-verify`（跳过 hook = 绕过平台服务端校验）、`--author` / `--date`（**伪造"这个改动是用户做的"**）、`-C` / `-c`（传任意 git config）、`--gpg-sign` / `--no-gpg-sign` / `-S` / `--signoff` / `--fixup` / `--squash`。

### 5.3 输出脱敏（云端刚需，最易漏）

需要覆盖的正则（含 wuu 的实现）：
- `Authorization: Bearer xxx` / 裸 `bearer xxx`
- `api_key|access_token|refresh_token|password|secret` + `[:=]` + 值
- **★ URL 内嵌凭据：`(https?|ssh|git)://[^/\s:@]+:[^@\s/]+@`**

最后一条是重点：若凭据以 `https://x-access-token:${TOKEN}@github.com/...` 形式注入，`git remote -v`、报错 stderr 都会**原样吐出 token**，然后被模型看到、写进日志、甚至推回远端。方案 ③ + §4.3-P2 天然规避了 URL 形式，但正则仍要作为**兜底**保留（纵深防御）。

### 5.4 非交互环境（防沙箱卡死）

`GIT_TERMINAL_PROMPT=0`（**必须有**：否则需要认证的 `git pull` 会挂住等 stdin，任务空转、计费继续）、`GIT_EDITOR` / `GIT_SEQUENCE_EDITOR` / `EDITOR` / `VISUAL` = `true`、`GIT_PAGER` / `PAGER` / `GH_PAGER` = `cat`、`NO_COLOR=1`。
**注意**：必须**覆盖式合并**（`merge(environ, override)`）而非追加，否则会被外层环境变量反制。

### 5.5 SSRF 校验（clone URL）

复用现有 `userconfig/mcp.py:110-115` 的实现（localhost / loopback / private / link_local / unspecified）。**额外补充**：云元数据端点必须显式拒绝——`169.254.169.254`（AWS/GCP/Azure 元数据服务）以及各云厂商的元数据域名。

### 5.6 敏感路径保护：`.git`（**本轮新发现的缺口**）

事实 §二-3：**工具层现在没有任何敏感路径保护**。切到 Volume 可写挂载后：

| 风险 | 后果 |
|---|---|
| Agent 读 `.git/**` | 可读历史、remote URL、hook 配置（探测平台配置） |
| Agent 写/删 `.git/**` | **可直接破坏仓库**（`rm -rf .git`、改 HEAD、改 hook）→ git 远程管理能力被釜底抽薪 |

**修法（按 wuu"敏感路径独立于沙箱、所有模式生效"的原则）**：
1. 在 Agent 侧文件工具加**拒绝清单**：`.git/`、`.env`、`.env.*`、`id_rsa`、`credentials*`（复用 §3.1 D1 已建好的 `_PATH_FIELDS` 单一卡点，天然覆盖所有文件工具与未来新工具）
2. **策略按沙箱类型区分**：同一份拒绝清单在 Agent 沙箱生效，在 runner 沙箱**不生效**（runner 必须能写 `.git`）
3. **诚实边界**：`run_command` 仍可 `rm -rf .git` ——工具层拦不住，彻底解决依赖"模板级 OS 约束"（已列为独立立项项）

### 5.7 fail-closed（不静默降级）

沿用已确立的原则：沙箱/runner 起不来 → 明确报错；白名单下发失败 → 显式告警而非假装已收紧；凭据校验失败 → 拒绝操作而非降级重试。

---

## 六、Git runner 沙箱设计（方案 ③）

```
                    ┌─────────────────────────────┐
租户 / Agent ──────▶│ Agent 沙箱                   │  无凭据、无 git 能力
                    │  挂载 Volume（可写）          │  敏感路径保护：禁 .git/**
                    │  Agent 在此改代码             │
                    └──────────┬──────────────────┘
                               │ 同一 Volume（挂载点不同、无共享进程空间）
                    ┌──────────▼──────────────────┐
平台后端 ──────────▶│ git runner 沙箱               │  持凭据（短命）
（可信侧发起）       │  挂载 Volume（可写）          │  git 命令白名单三层校验
                    │  clone / commit / push        │  输出脱敏 → 返回后端
                    │  用完即毁                     │  记审计
                    └──────────────────────────────┘
```

**为什么选它**（对应你 Q2 的倾向）：
- **凭据与 Agent 环境物理隔离**：不同 VM、Agent 不可达 → T1 从"难以防范"变成"结构上不可能"
- **零搬运**：runner 与 Agent 挂同一 Volume，不需要"Volume → 临时目录 → git"（方案 ② 的搬运成本与临时盘负担在此消失）
- **能力对齐**：Agent 改文件 → 直接在 Volume；runner 跑 git → 也在 Volume；没有第三份副本
- **可演进**：将来把凭据换成 E2B 出口代理注入（§4.3-(b)），架构不用动

**成本**：多一个沙箱生命周期与编排（创建/挂载/销毁），需纳入工作区互斥租约（同一 workspace 的 git 操作串行）。

---

## 七、多租户分层（wuu 给不了答案，自研）

| 层 | 内容 |
|---|---|
| **密钥分层** | **平台资产**：E2B_API_KEY、COS 密钥、平台模型 key → 租户不可见、不可注入、地板保护（P6）。**租户资产**：git 凭据、租户自己的模型 key → 加密存储、按 `user_id` 隔离、可撤销 |
| **配额** | 沙箱并发上限、Volume 容量、git 操作速率、runner 沙箱生命周期（防被当免费算力）、单次 clone 大小上限 |
| **隔离现状** | ✅ Volume 命名含租户前缀；✅ `list_mounts(user_id)` 只挂自己的；✅ 工具层跨工作区已禁（D1）。**待补**：配额与用量归集 |
| **计费** | 已有 usage 模块；需明确 git 操作（clone 流量、runner 时长）是否计入 |

---

## 八、分阶段实施与验证

| 阶段 | 内容 | 验证 |
|---|---|---|
| **M1** | 存储后端默认切 `e2b`；local 线收敛为可选 backend；确认同步层不再触发 | 新建工作区 → 上传文件 → 沙箱内可见（挂载生效）；日志无 B2/B3 同步记录 |
| **M2** | git **只读**：clone / status / log / diff；命令白名单三层校验；输出脱敏；非交互环境；SSRF 校验 | 单测覆盖：`--exec-path` 等被拒、URL 内嵌 token 被脱敏、`169.254.169.254` 被拒、非交互 env 覆盖生效 |
| **M3** | git **写**：commit / push / pull；`blockedCommitFlags`；runner 沙箱；凭据托管（先 (a) 注入） | Agent 沙箱内 `cat .git/config` / `env` 无 token；runner 执行 push 成功；`--no-verify` 被拒；审计日志完整且无 token |
| **M4** | 敏感路径保护（`.git`/`.env`）；多租户配额与密钥分层 | `.git` 读写被工具层拒绝；runner 侧不受限；配额超限被拒 |

**依赖**：M2/M3 需要后端或 runner 具备 git（当前 E2B 模板未显式安装 git，沙箱内可用性未验证 → M2 前置需确认）。

---

## 九、已定 / 待定

**已定（本轮）**
1. 本地目录线：**保留代码，收敛为独立 backend 实现**；只改默认配置为 `e2b`
2. Git 执行位置：**③ runner 沙箱**（后端控制、Agent 不可达）
3. Git 范围：**先 clone + commit + push 打通**；分支切换 / PR 创建后续再做；危险操作一律不给
4. 借鉴项：三层命令白名单、`blockedCommitFlags`、输出脱敏、非交互环境、SSRF、地板保护、fail-closed —— 全部执行
5. 同步层：随 `e2b` 后端切换后**不再触发**（代码与测试保留在 backend 层）

**待定**
- 多租户配额与密钥分层是否随 M4 一起做（还是等单租户 git 打通后）
- 凭据注入点：(a) 后端 `envs=` 注入 / (b) E2B 出口代理（建议先 a 后 b）
- 云端出口白名单基线内容（哪些域名默认放行）
- `resume.md` 第 8 条（本地目录双向同步）需改写为新定位叙事
