# skills（技能目录、存储门面与 HTTP）

## 第一部分：模块名称与简介

**模块名**：`skills`

**一句话定位**：加载平台内置 Skill 目录、经存储门面（`storage`）读取**平台级 / 用户级 / 项目级**三层 Skill，裁决同名冲突后渲染成 System Instruction 的一段，并通过只读 HTTP 接口把三层状态提供给浏览器。Skill 文件存储后端（本地目录 / E2B Volume）可通过环境变量切换，上层不感知。

**对应 Go 版源码路径**：`backend-v2/internal/skills/`（`catalog.go`、`http.go`、`module.go`、`types.go`，以及 `system/` 下的内置 Skill 文件）。

**Skill 作用域（当前实现）**：

| 层级 | 是否支持 | 隔离维度 | 载体 |
| --- | --- | --- | --- |
| 平台级（内置） | ✅ 支持 | 无，所有用户共享、只读 | 源码 `system/`；模板构建时 COPY 进沙箱 |
| 用户级 | ✅ 支持 | `user_id` | `user-skills/{user_id}/`（local）或 E2B Volume（e2b） |
| 项目级（Workspace） | ✅ 支持（**仅 local**） | `(user_id, workspace_id)` | `project-skills/{user_id}/{workspace_id}/`，沙箱侧 `skills/project/{workspace_id}/` |
| 会话级（Session） | ❌ **不做**（理由见设计稿第三节） | — | — |

> 三层如何裁决同名、为什么"覆盖是视图级"、删项目级后用户级如何自动恢复，见 `docs/skill-scope-design.md`（**D1–D11 已冻结并实施到 B2-3**）。

三条需要写清楚的事实：

- 存储后端的方法签名**只收 `user_id`**（用户级调用点不该被迫传一个假的 `workspace_id`）。项目级后端在**构造时**绑定 `workspace_id`，一个实例只服务一个项目；`SkillScopeStores` 负责按 workspace 缓存实例（否则基线指纹与冲突快照每次归零）。
- 项目级**首期只支持 local**：`e2b` 的 `mount_spec(user_id)` 只能挂一个用户卷，而 Volumes 目前是 private beta（`POST /volumes` 恒 403）。`e2b` 下项目层返回 `unavailable` + 原因，**不假装为空**。
- 项目级**记忆**（`workspace/memory.py`，注入为【项目背景】）与 Skill 是两套独立机制，前者按 `workspace_id`、后者按"作用域 + `user_id`"，不要互相套用。

**文件清单**：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 模块出口：导出 `HTTP`、`Catalog`、`Skill`、`SkillSummary`、`SkillListItem`、`SkillsResponse`、`load_catalog`、`parse_user_overview` |
| `catalog.py` | 内置 Skill 加载（扫描 `system/`）与用户索引读取；`Catalog` 目录服务类，依赖存储门面而非具体后端 |
| `resolver.py` | 作用域解析（**B2-2 已实施**）：`SkillResolver.resolve()` 把平台 / 用户 / 项目三层收敛成唯一产物 `ResolvedCatalog`（`entries` 带 `scope`/`sandbox_path` 且 **name 唯一**，`shadows`/`conflicts` 走状态侧信道）；只读不写、不缓存 |
| `budget.py` | 两层预算（**B2-3 已实施**）：L1 `plan_index()` 纯函数（按 scope 份额 + 溢出再分配，确定性截断）；L2 `SkillLoadBudget` + `ContextVar` 账本（3 个 Skill / 60KB，48KB 预警，拒绝返回结构化结论）。只放策略，不碰渲染与沙箱 |
| `migrate.py` | 存量 Skill 显式迁移（**B3 已实施**，命令 `edith-skill-migrate`）：5-A 单条（`--user/--skill/--to`）+ 5-B 批量（`--file migration.yaml`），默认 dry-run。整批 6 项预检查 → 逐条 copy → sha256 校验 → 目录级 rename → 刷索引；**源永不自动删、不整体回滚、重跑幂等**。这是运维 CLI，不在 HTTP 链路上 |
| `http.py` | HTTP 边界：`GET /internal/skills` 只读列表接口（含存储状态字段；传 `workspaceId` 才返回 `project*` / `shadows` / `conflicts`） |
| `types.py` | 类型定义（`Skill`、`SkillSummary` 为 dataclass；`SkillListItem`、`SkillsResponse` 为 Pydantic 模型）与 Sandbox 相对路径常量 `SYSTEM_PATH`/`CUSTOM_PATH`/`PROJECT_PATH` |
| `storage/__init__.py` | 工厂：`new_skill_backend(kind, volumes=None)`，`kind ∈ {local, e2b}`，未知值回退 local 并告警 |
| `storage/base.py` | `SkillStorageBackend` 抽象基类 + `MountSpec`/`SkillIndex`/`SkillSyncResult` + 状态常量 + `SANDBOX_SKILLS_PATH` |
| `storage/local.py` | 默认后端：权威副本 = 宿主目录 `user-skills/{user_id}/`；索引扫描自愈、原子写、冲突快照；沙箱目录由 `sandbox_mkdir_chain()` **逐级**创建（模板只建了 `skills/{system,custom}`，项目级两级父目录不存在，不能押远端 `make_dir` 递归补） |
| `storage/e2b.py` | 可选后端：把既有 `volume/service.py` 包成同一抽象（挂载即共享，`push`/`pull` 走基类默认 no-op） |
| `storage/service.py` | 门面：per-user 锁、指纹快路径、状态归一化与单行同步日志 |
| `storage/project.py` | 项目级后端：**独立根** `project-skills/{user_id}/{workspace_id}/`，沙箱路径 `skills/project/{workspace_id}`，共识集合前缀 `project-skills-{workspace_id}-`（机制全继承 `local`） |
| `storage/scopes.py` | 作用域分发：`new_scope_backend(scope, kind, workspace_id, volumes, access_checker)` + `SkillScopeStores`（项目级门面按 workspace 缓存，否则基线/快路径每次归零） |
| `storage/access.py` | 项目级归属校验的唯一入口 `assert_access(user_id, workspace_id)`；**fail-closed**（未注入判定器 = 拒绝），判定用 `WorkspaceStore.exists`（`SELECT 1`），不缓存、不区分「不存在」与「无权」 |
| `storage/transaction.py` | D9 的目录级替换协议：`.staging-<ts>/` → 校验 → 按顶层 Skill 目录整体替换 → `.old-<ts>` 崩溃可恢复；被拒候选进 `.rejected/<ts>/` |
| `storage/validate.py` | 同步前四类校验（格式 / 保留名 / 数量 / 体积）：`errors` 整份拒绝、`warnings` 放行上报；`platform_skill_names()` 现扫不缓存 |
| `system/current-time/SKILL.md`、`edith.yaml` | 内置公共 Skill：需要当前时间时调用 `get_current_time` 工具 |
| `system/skill-creator/SKILL.md`、`edith.yaml` | 内置公共 Skill：按 EDITH 规范创建/维护用户 Skill 的工作方法 |
| `system/skill-creator/scripts/init_skill.py` | 创建用户 Skill 骨架（`SKILL.md` + `edith.yaml` + 可选资源目录），仅用 Python 标准库 |
| `system/skill-creator/scripts/quick_validate.py` | 校验一个 Skill 目录的格式（frontmatter、name/description 约束、edith.yaml），仅用 Python 标准库 |
| `system/skill-creator/scripts/sync_overview.py` | 扫描用户 Skills 目录并生成 `overview.md` 摘要索引，仅用 Python 标准库 |

## 第二部分：架构设计

### ① 分层

| 层次 | 载体 | 说明 |
| --- | --- | --- |
| HTTP 边界层 | `http.py` 的 `HTTP` 类 | 持有 FastAPI `APIRouter`，只暴露 `GET /internal/skills` 只读接口；校验 `userId`，组装响应模型与存储状态字段 |
| 服务/目录层 | `catalog.py` 的 `Catalog` 类 + 模块级函数 `load_catalog` / `parse_user_overview` | 内置 Skill 的内存目录；用户索引经存储门面读取，自身不感知 Local / E2B |
| 作用域解析层 | `resolver.py` 的 `SkillResolver` + `ResolvedCatalog` | 「哪些 Skill 生效」的唯一落点：三层在此裁决成 `entries`（带 `scope`/`sandbox_path`，name 唯一）；优先级是**数据结构性质**，不靠注入顺序推断。只读不写、不缓存，每次 run 重算（D3 的「删项目级自动恢复」就建立在这一点上） |
| 存储门面层 | `storage/service.py` 的 `SkillStorageService` | 唯一对外的存储入口：加锁、指纹快路径、把后端异常归一为 `status`/`reason`、输出单行同步日志 |
| 存储后端层 | `storage/local.py` / `storage/project.py` / `storage/e2b.py` | 同构实现 `SkillStorageBackend`；`local` 为默认，`project` 继承 `local` 只换根目录/沙箱路径/状态键前缀，`e2b` 为保留能力（账号未开通时不可用；项目级在 `e2b` 下明确返回 `unavailable`） |
| 类型定义层 | `types.py` + `storage/base.py` | dataclass（内部）与 Pydantic（HTTP 契约）分离；路径常量 `SYSTEM_PATH = "skills/system"`、`CUSTOM_PATH = "skills/custom"`、`SANDBOX_SKILLS_PATH = /home/user/skills/custom` |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `catalog.py:101`（`Catalog.__init__(storage=None)`）、`http.py:16`（`HTTP.__init__(catalog)`） | 组合根 `main.py:190` `skills = SkillsCatalog(skill_storage)` 显式装配；`Catalog` 对 `storage` 为可选依赖（`None` 时返回 `status=empty, reason="skill storage not configured"`，不伪装成「读到了空目录」） |
| 抽象工厂 + 策略 | `storage/__init__.py:40` `new_skill_backend(kind, volumes=None)` | 按 `EDITH_USER_SKILLS_BACKEND` 选择后端；`local` 不需要额外依赖，`e2b` 需注入 `volume.Service` |
| 门面（Facade） | `storage/service.py:41` `SkillStorageService` | 上层只依赖门面：`ensure`/`mount_spec`/`read_index`/`push`/`pull`/`last_index`/`backend_name`，拿不到 backend 类型 |
| 模板方法 | `storage/base.py:91` `SkillStorageBackend` | 仅 `mount_spec`/`read_index` 为 `@abstractmethod`；`ensure`/`push`/`pull`/`local_fingerprint` 有默认实现（挂载型后端天然跳过同步） |
| 工厂 | `http.py:20` `HTTP._build_router()` | 在构造时构建并返回 `APIRouter`，路由闭包捕获 `self.catalog` |
| 适配器 | `catalog.py:77` `parse_user_overview` | 把索引原文（markdown）适配为 `list[SkillSummary]`；`storage/service.py` 在结构化摘要缺失时也走同一解析，两条路得到同一份列表 |
| 策略（简单排序） | `catalog.py:73`、`catalog.py:94` | 内置 Skill 与用户摘要均按 `name` 排序，保证 `list_system_summaries`/`parse_user_overview` 输出稳定 |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责/关键签名 |
| --- | --- | --- |
| `Catalog` | `catalog.py:98` | 目录服务：`__init__(storage=None)`；`list_system_summaries() -> list[SkillSummary]`（内存，稳定顺序）；`async read_index(user_id) -> SkillIndex`（含状态与原因）；`async list_user_summaries(user_id) -> list[SkillSummary]`；`async read_user_overview(user_id) -> str`（保留的旧签名，当前无调用方） |
| `load_catalog()` | `catalog.py:66` | 扫描 `_SYSTEM_DIR`（`catalog.py:23`，本文件旁 `system/`），逐个加载含 `SKILL.md` 的目录，按 `name` 排序返回 `list[Skill]` |
| `_load_skill(skill_dir)` | `catalog.py:38` | 解析单个 Skill：`SKILL.md` frontmatter（必填 `name`/`description`）+ 可选 `edith.yaml`（`display_name`/`short_description` 回退到 name/description）；正文写入 `Skill.body`，但**当前无任何消费者**（正文不进 prompt） |
| `_split_frontmatter(content)` | `catalog.py:26` | 分离 SKILL.md 的 YAML 头部与正文，兼容 BOM（`\uFEFF`）；格式错误抛 `ValueError` |
| `parse_user_overview(content)` | `catalog.py:77` | 解析 `sync_overview.py` 生成的摘要行（`- \`name\`：desc`），按 `name` 排序 |
| `HTTP` | `http.py:24` | HTTP 边界：`__init__(catalog, scopes=None)`（`scopes` 只用于读项目层索引，权限判定仍在存储层）；`_build_router() -> APIRouter`；路由 `GET /internal/skills`（query 参数 `userId` 必填、`workspaceId` 可选） |
| `ProjectSkillBackend` | `storage/project.py` | 项目级后端：构造时绑定 `workspace_id`（方法签名仍是 `user_id`，不逼用户级调用点传假 workspace）；`user_dir()` → `{root}/{user_id}/{workspace_id}/`；空 `workspace_id` 抛 `ValueError`（不是静默返回空存储） |
| `SkillScopeStores` | `storage/scopes.py` | `user()` / `project(workspace_id)`；项目级门面按 workspace 缓存——`SkillStorageService` 的锁、指纹与后端的基线都会累积，每次新建等于每次丢掉冲突快照 |
| `new_scope_backend` | `storage/scopes.py` | `(scope, kind, workspace_id, volumes) -> SkillStorageBackend`；未知 scope / `e2b`+project 返回 `_UnavailableBackend`（`status=unavailable` + 原因），不降级成用户级、不假装为空 |
| `SkillStorageService` | `storage/service.py:41` | 存储门面：`async read_index(user_id) -> SkillIndex`（per-user 锁内执行）、`async push/pull(user_id, sandbox) -> SkillSyncResult`、`async last_index(user_id)`、`backend_name` |
| `SkillStorageBackend` | `storage/base.py:91` | 存储抽象：`ensure`/`mount_spec`/`read_index`/`push`/`pull`/`local_fingerprint`，**签名只含 `user_id`**（无 workspace/session 维度） |
| `LocalSkillBackend` | `storage/local.py:167` | 默认后端（也是项目级后端的基类）：`__init__(root, *, sandbox_root, state_prefix)` 三个参数化点；`user_dir(user_id)`（`:205`）→ `{root}/{user_id}`；`state_key(user_id)`（`:208`）→ 删除传播共识集合键；`read_index`（`:246`）扫描目录并自愈索引 |
| `E2BSkillBackend` | `storage/e2b.py:31` | 可选后端：`mount_spec`（`:39`）经 `volume.Service.mount_for_user` 返回挂载名；`read_index`（`:47`）读卷内 `/overview.md`，把「卷未创建」判为 `empty`、「连接失败」判为 `unavailable` |
| `SkillResolver` | `resolver.py:151` | 作用域解析：`async resolve(user_id, workspace_id=None) -> ResolvedCatalog`（`:164`）。读三层后交给 `_adjudicate`（`:272`）裁决；`workspace_id` 为空时项目层按 R0 不成立处理（不抛错、不假装为空存储） |
| `ResolvedCatalog` / `ResolvedEntry` | `resolver.py:129` / `:79` | 解析产物：`entries[]`（`name`/`description`/`scope`/`sandbox_path`/`source_dir`，**name 唯一**）、`reports{scope: ScopeReport}`（`status`/`reason`/`healed`/`count`，`count` 是裁决**前**的条数）、`shadows[]`/`conflicts[]`（`ScopeDecision`，**只描述本次怎么裁，不是删除依据**）。B2-3 起**不再携带索引原文**（`user_overview` 已删）：注入统一从 `entries` 渲染 |
| `ScopeDecision` | `resolver.py:104` | 一条裁决记录：`name`/`winner_scope`/`loser_scope`/`reason`/`detail`。`shadows`（规则内覆盖）与 `conflicts`（需人介入）共用结构、按语义分列；原因字符串是稳定契约（`resolver.py:73-75`） |
| `plan_index(costs)` | `budget.py` | L1 纯函数：`[(scope, name, 字符成本)]` → `IndexPlan(kept=原下标, omitted=[(scope,name)])`。两轮分配（份额保底 → 公共池按优先级补），顺序固定因此结果确定 |
| `SkillLoadBudget` | `budget.py` | L2 账本：`charge(key, size) -> ChargeDecision`（`allowed`/`reason`/`message`）、`snapshot()` → `{loadedSkills, loadedBytes, denied}`。数量按不同 Skill 数、体积按累计返回字节 |
| `bind_skill_load_budget` / `charge_skill_load` | `budget.py` | 账本挂在 `ContextVar` 上（作用域 = run 的 task）。绑定在 `agentrun/execution.chat` 入口；未绑定时 `charge_skill_load` 返回 `None`（不计量） |
| `SkillIndex` / `SkillSyncResult` | `storage/base.py:58` / `:73` | 索引结果（`text`/`summaries`/`status`/`reason`/`healed`）/ 同步结果（`pushed`/`pulled`/`deleted`/`conflicts`/`rejected`/`status`/`reason`/`skipped`） |
| `SkillAccessDenied` | `storage/access.py` | 刻意**不继承 `OSError`**：`read_index`/`push`/`pull` 用 `except (OSError, ValueError)` 兜底本地文件错误，权限拒绝若落进那个分支会被写成「ensure skill dir failed」这类误导性原因 |
| `validate_skill_tree` | `storage/validate.py` | `(root, reserved=..., max_count=..., max_skill_md_bytes=..., max_dir_bytes=...) -> ValidationResult`；目录名必须等于 frontmatter `name`（B2 的 `sandbox_path` 由目录名拼出） |
| `commit` / `recover` / `reject` / `purge_stale_staging` | `storage/transaction.py` | 替换 = 「暂存有的搬进去 + 权威有而暂存没有的**目录**删掉」；`recover()` 在每次 `ensure()` 前跑，处理 `.old-<ts>` 残留；`commit` 失败**不清理 `.old`**（那是唯一副本）；清理统一走 `_remove_tree()`（失败不上抛），过期暂存按年龄回收 |
| `Skill` / `SkillSummary` | `types.py:33` / `types.py:45` | dataclass：解析完成的 Skill（含 `body`/`display_name`/`short_description`）/ 传给 AgentRun 的最小摘要（**`dir_name` 是真实目录名**，索引路径与 `sandbox_path` 都必须用它） |
| `SkillListItem` / `SkillsResponse` | `types.py:59` / `types.py:66` | Pydantic 模型：浏览器展示的摘要项 / `{system, custom, customStatus, customReason, project*, shadows, conflicts}` 列表 HTTP 输出 |
| `SYSTEM_PATH` / `CUSTOM_PATH` / `PROJECT_PATH` | `types.py:13-18` | 常量：Sandbox 文件工具使用的公共/用户/项目 Skills 相对路径；`IMPLEMENTED_SCOPES`（`:29`）决定 resolver 读哪几层 |
| `create_skill(name, root, resources, display_name, short_description)` | `init_skill.py:36` | 创建 Skill 骨架（`SKILL.md`、`edith.yaml`、`scripts`/`references`/`assets` 资源目录）；目录已存在抛 `ValueError` |
| `validate_skill(skill_path)` | `quick_validate.py:48` | 校验 Skill 目录，返回 `(是否通过, 中文消息)`；覆盖 frontmatter 完整性、name 命名规则（小写字母/数字/单连字符）、目录名与 name 一致、长度上限、`<`/`>` 字符、edith.yaml 键约束 |
| `build_overview(root)` | `sync_overview.py:31` | 扫描 custom 目录，为每个 Skill 生成 `- \`name\`：description` 与路径行，输出 `overview.md` 文本 |

## 第三部分：与其他模块的联系

### ① 依赖关系

**本模块 import 的内部模块/外部库**：

| 依赖 | 用途 |
| --- | --- |
| `..http_utils.EDITHError`（`http.py:10`） | 抛出统一业务错误（status + type + message 三元组），由 `http_utils.register_error_handler` 转为 `{"type","message"}` JSON |
| `..workspace.sync`（`storage/local.py`） | 复用 `sync_local_to_sandbox` / `sync_sandbox_to_local` / `scan_local_fingerprint`，以 `mount=SANDBOX_SKILLS_PATH` 参数化，不复制同步护栏 |
| `volume.Service`（**仅 e2b 后端**，构造器注入） | `E2BSkillBackend` 经 `mount_for_user` / `open_for_user` 读写 E2B Volume；`local` 后端完全不依赖它 |
| `PyYAML`（`catalog.py:16`，`yaml.safe_load`） | 解析 `SKILL.md` frontmatter 与 `edith.yaml` |
| `fastapi.APIRouter`、`pydantic.BaseModel` | HTTP 路由与响应模型 |

**谁依赖本模块**：

- 组合根 `main.py`：`from .internal.skills.storage import SkillScopeStores, SkillStorageService, new_skill_backend, workspace_access_checker`（`main.py:52`）；`skill_storage = SkillStorageService(new_skill_backend(os.getenv("EDITH_USER_SKILLS_BACKEND", "local"), volumes))`（`main.py:172`）；`skill_scopes = SkillScopeStores(..., user_service=skill_storage, access_checker=workspace_access_checker(workspaces))`（`main.py:178-181`，项目级归属判定的唯一来源）；`skills = SkillsCatalog(skill_storage)`（`main.py:203`）；注入 `Dependencies(skills=skills, skill_scopes=skill_scopes, ...)` 给 agentrun；`SandboxService(..., skill_scopes=skill_scopes)`（`main.py:184`）；`app.include_router(SkillsHTTP(skills, skill_scopes).router)`（`main.py:254`）
- `agentrun/configuration.py`：`RunConfigurations` 持有 `skills: SkillsCatalog | None`（`configuration.py:94`）与 `skill_scopes: SkillScopeStores | None`（`:100`），构造时装配解析器 `SkillResolver(skills, skill_scopes)`（`:109-111`）；运行时调 `await self.skill_resolver.resolve(user_id, workspace_id)`（`:178`）拿到 `ResolvedCatalog`（三层状态 + `shadows`/`conflicts` 全部落 trace 的 `skills.summaries` 步骤），再由 `render_skill_instruction()` 渲染（`skills.inject` 步骤记 `injected`/`omitted`/`budgetChars`）注入 System Instruction。**configuration 不再直接调 `list_system_summaries`/`read_index`**——三层的读取与裁决只在 `resolver.py` 里发生一次；渲染与预算只在 `render_skill_instruction` 里发生一次
- `sandbox/service.py`：构造时注入 `skill_scopes: SkillScopeStores | None`（只收分发点，不收两个门面——「什么时候算项目级」必须只有一处规则）；`_scope_service(scope, workspace_id)`（`:274`）按作用域取门面（无工作区 / 后端结构性不可用 → None）；`_push_skills`（`:322`）两个分支都调用（`:488` 复用分支、`:556` 新建分支）；`pull_skills`（`:347`）/ `mark_skill_pull_pending`（`:365`）/ `flush_skill_sync`（`:406`）；新建沙箱时用 `mount_spec`（`:499`）决定是否挂卷
- `sandbox/tools.py`：写类工具后判定命中了**哪一层**作用域（`_touched_skill_scopes`，`:128`；路径按目录段匹配 `skills/custom` / `skills/project`，命令标记 `skill-creator`/`SKILL.md`/`skills/project`），命中即按作用域拉、未命中连 `workspace_id` 一起挂起（`:543-556`）
- `agentrun/service.py`：`Dependencies.skill_flush`（`:90`）在 run 收尾时调用（`:612-614`），兜底覆盖启发式判断不出的写入

### ② 数据流向

**典型业务流转 A：浏览器拉取 Skills 列表**

```text
Web BFF（Next.js）
  └─ GET /internal/skills?userId=xxx            （http.py:23）
       ├─ 校验 userId：为空 → EDITHError(400, "invalid_request", "userId is required")
       ├─ catalog.list_system_summaries()       （内存，内置 Skill 摘要）
       └─ await catalog.read_index(userId)      （catalog.py:111）
            └─ SkillStorageService.read_index   （storage/service.py:77，per-user 锁内）
                 └─ LocalSkillBackend.read_index（storage/local.py:246）
                      ① 扫描 user-skills/{uid}/ 下各 SKILL.md frontmatter
                      ② 与 overview.md 比对，不一致 → 原子回写（healed=True）
                      ③ 返回 SkillIndex{text, summaries, status, reason}
       └─ SkillsResponse{system, custom, customStatus, customReason, project*, shadows, conflicts} → JSON
```

**典型业务流转 B：AgentRun 一次运行的指令注入**

```text
agentrun.RunConfigurations.load(request)
  ├─ await skill_resolver.resolve(user_id, workspace_id)   （configuration.py:172）
  │     └─ SkillResolver.resolve（resolver.py:164）
  │          ├─ 平台层：catalog.list_system_summaries() → entries[scope=platform]
  │          ├─ 用户层：catalog.read_index(user_id) → entries[scope=user]
  │          ├─ 项目层：skill_scopes.project(ws).read_index(user_id) → entries[scope=project]
  │          │     （无 workspace / 未注入分发点 → 不成立，R0；无权 → status=unavailable，R5）
  │          ├─ _adjudicate（resolver.py:272）：R4 同层去重 → R2 淘汰保留名 → R3 项目覆盖用户
  │          └─ trace step `skills.summaries` 记录 system / user / userStatus / userReason /
  │             userHealed / project / projectStatus / projectReason / projectHealed / shadows / conflicts
  │             （`count` 取**解析出的条数**，不是后端 summaries 长度——e2b 只给索引原文时旧口径会记 0）
  ├─ render_skill_instruction(resolved) → trace step `skills.inject`（configuration.py）
  │     ├─ plan_index（budget.py）：按 scope 份额 + 溢出再分配，确定性截断到 4000 字符
  │     ├─ 每条一行：`- [platform|project|user] name：description`，目录名≠name 时补「（目录：x）」
  │     ├─ 被截断时文本里写"另有 N 个 Skill 未列出"（不写名字：名字本身也占预算）
  │     └─ 末尾注明三层的正文路径与 L2 额度（同轮最多 3 个 / 60KB）
  └─ system_instruction += "\n\n" + instruction（**追加在末尾**，开头改字会毁掉前缀缓存）
```

**典型业务流转 C：Agent 读一个 Skill 的正文（L2 预算）**

```text
agentrun.execution.chat 入口: bind_skill_load_budget(SkillLoadBudget())   ← ContextVar，作用域 = 本 run 的 task
  └─ sandbox_read_file → _op_sandbox_read_file
        ├─ skill_body_key(path)：只认 skills/{system,custom}/{dir}/SKILL.md 与 skills/project/{ws}/{dir}/SKILL.md
        ├─ charge_skill_load(key, 返回字节)：数量按不同 Skill 数、体积按累计返回字节
        ├─ 放行 → 正常返回；越 48KB 附带预警文案
        └─ 拒绝 → {error, kind: command_error, retryable: false, skill, budget, suggestion}（不抛异常）
  收尾: skill_budget.snapshot() 并入 trace `completion` 的 loadedSkills / loadedBytes / denied
```

完整 Skill 正文**不**进入指令：Agent 在沙箱内通过 Sandbox 文件工具按需读取。两侧文件来源不同：

- 平台级 `skills/system/`：**模板构建时**由 `e2b-template/edith-sandbox-python/template.py:28` 的 `.copy(SKILLS_SOURCE, "/home/user/skills/system/")` 写入镜像（`SKILLS_SOURCE` 指向本仓库 `skills/system`，含 `skill-creator/scripts/`）。改 `SKILL.md` 后**必须重建模板**，否则 prompt 里的 `description`（运行时读盘）与沙箱里的正文（镜像内）会不一致。
- 用户级 `skills/custom/`：local 后端由 `push` 从 `user-skills/{uid}/` 填充；e2b 后端由卷挂载提供。

### ③ 接口调用

**对外 HTTP 路由**：

| Method | Path | 参数 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| `GET` | `/internal/skills` | query `userId`（必填）、`workspaceId`（可选） | `SkillsResponse`：`{ "system": [{name, description}], "custom": [...], "customStatus": "ok\|empty\|partial\|unavailable", "customReason": "", "project": [...], "projectStatus": "", "projectReason": "", "shadows": [{name, winner, loser, reason, detail?}], "conflicts": [...] }` | 只读列表；`userId` 缺失返回 400 `invalid_request` |

`customStatus` / `customReason` 是向后兼容的新增字段：`custom` 为空数组时，前端据此区分「用户确实没有 Skill」（`empty`）与「存储不可用」（`unavailable`）。

`project*` / `shadows` / `conflicts` 同理（B2-2），但两个语义边界必须守住：

- **`custom` 仍是「你拥有的用户 Skill」**，不随 `workspaceId` 变化、也不做覆盖裁决——否则设置页里会突然少掉几条，看起来像丢数据；
- 要回答「这个项目里哪一条生效」，看 `shadows`（`winner=project` 表示该条被项目级压住），不要在 `custom` 里筛。

- 注册点：`main.py:254` `app.include_router(SkillsHTTP(skills, skill_scopes).router)`（挂在整个 FastAPI 应用上，路径前缀即 `/internal/skills`）
- 调用方：Web BFF（Next.js）转发 `GET /api/skills` → 后端 `GET /internal/skills`

**对外内部方法（`Catalog` / 门面）**：

| 方法 | 签名 | 调用方 |
| --- | --- | --- |
| `list_system_summaries` | `() -> list[SkillSummary]` | `http.py:47`、`resolver.py`（平台层） |
| `read_index` | `async (user_id: str) -> SkillIndex` | `http.py:40`、`resolver.py`（用户层） |
| `list_user_summaries` | `async (user_id: str) -> list[SkillSummary]` | 暂无直接调用方（HTTP 走 `read_index`） |
| `read_user_overview` | `async (user_id: str) -> str` | 无（保留的旧签名，等价于 `read_index(...).text`） |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

- **公共 Skills 启动时全量加载**：`Catalog` 构造时 `load_catalog()` 从源码树 `system/`（`catalog.py:23`）一次性解析全部含 `SKILL.md` 的目录到内存，之后只读；Go 版用 `embed.FS` 内嵌，Python 版直接读源码目录（运行期依赖安装包携带这些数据文件）。
- **SKILL.md 契约**：必须以 `---` 开头、frontmatter 至少含 `name` 与 `description`（缺失抛 `ValueError`）；`edith.yaml` 可选，仅提供 `display_name`/`short_description` 展示字段（当前无消费者，属预留）。
- **摘要与正文分离（渐进加载）**：指令只注入摘要（实测约 340 字符）；完整正文由 Agent 通过 Sandbox 文件工具按需读取（`sandbox_read_file` 支持 `offset`/`limit`，默认 200 行）。
- **pull 是「暂存 → 校验 → 目录级替换」，不是逐文件覆盖**（D9）：逐文件写的后果是「模型把 `SKILL.md` 写坏 → 权威副本也坏了 → 下一轮索引里该 Skill 直接消失」。现在候选先落 `.staging-<ts>/`，校验不过就整份进 `.rejected/<ts>/`，旧的有效版本继续服务。三条「宁可不更新」的判定：单文件失败/熔断、`list` 失败、沙箱返回空树而本地有 Skill。
- **删除比覆盖危险一档，所以额外要授权**：替换的第二个方向（候选没有的目录随之删除）只在 `in_sync` 为真时执行——即「本地与这个沙箱上次成功同步之后指纹没变」，也就是**这份候选确实见过当前权威内容**。否则只做同名覆盖，旧目录原样留下，`reason` 报 `N deletion(s) suppressed`。被压制的回拉**不推进基线**，否则下一轮（run 开始时先拉后推）会拿失效证据放行删除。真实事故：一个 11-05 就没再同步过的沙箱回拉一次，把宿主 14 个 Skill 裁到 13，并且把删除经 push 传播给了其它沙箱。
- **Windows 上目录替换必须三步**（旧 → `.old-<ts>`、暂存 → 正式、删 `.old`）：POSIX 的 `rename(2)` 能原子替换已存在的目录，Windows 会失败。崩溃残留由 `ensure()` 每次访问前 `recover()`：正式目录缺失则搬回，两边都在则删旧残留。
- **提交之后的清理是尽力而为，不改变「数据有没有落地」的结论**：`.old-<ts>` / `.staging-<ts>` 删不掉（Windows 占用、环境注入的删除护栏）只留一份垃圾，`commit` 照常返回成功、`recover()` 下次再试。`shutil.rmtree(ignore_errors=True)` 在这里是错的——它只吞 `OSError`，`SystemExit` 会从 asyncio 任务里穿出去终止事件循环（真实事故：一次 pull 收尾把整个后端带下去）。所以统一走 `_remove_tree()`：整树删失败就退化成自底向上逐条删，单条失败只跳过它。过期的 `.staging-*` 由 `ensure()` 里的 `purge_stale_staging()` 按年龄（2 小时）回收——取消和崩溃都会留下它，看见就删会误杀并发 pull 的暂存。
- **归属校验在存储层，不在路径层**（7.5）：沙箱 `run_command` 可以 `cp`/`mv`/`rm`，完全不过工具层的路径校验，所以路径校验只是第二层。`ProjectSkillBackend.assert_access` 是唯一入口，未注入判定器时**拒绝**而不是放行。
- **索引是派生缓存，文件才是权威**：`overview.md` 由 `sync_overview.py` 生成，`parse_user_overview` 的解析格式与该脚本输出是隐式契约。`LocalSkillBackend.read_index` 读前会按目录重算，与 `overview.md` 不一致即原子回写并置 `healed=True`——不再依赖模型自觉运行脚本。`overview.md`、`.conflicts/`、`.rejected/`、`.staging-*`、`.old-*` 写进用户目录的 `.edithignore`，不参与同步——`_scan` 会跳过 `.` 开头的目录，但**同步不受 `_scan` 约束**，漏写会让暂存目录整个被 push 进沙箱。`is_ignored` 因此支持 `前缀*`（时间戳目录名写不成固定名）。
- **local 后端同步复用 workspace 的护栏**：只加了三个参数 —— `mount`（覆盖 `workspace/{id}` 推导）、`stats`（出参回填 `deleted`/`failures`/`aborted`/`list_failed`，把「没变化」与「整次失败」分开）、`verify_content`（逐字节比对）。**`verify_content` 是必须的**：sync 默认以「大小一致即未变」判等，会让 KB 级 markdown 的等长改写被漏掉。
- **同步触发三层**：沙箱获取时 push（本地未变走指纹快路径，0 网络）→ 写类工具命中 Skill 时立即 pull / 未命中则挂起 → run 收尾 `Dependencies.skill_flush` 兜底 pull。**Skill 钩子不带 `workspace_id` 条件**（纯聊天会话也要同步）。
- **项目级独立根 + 独立状态键**：`project-skills/{user_id}/{workspace_id}/`，共识集合前缀 `project-skills-{workspace_id}-`。三条理由都不是「目录整洁」：用户级 push 是整个根目录推走（嵌进去会把项目 Skill 推给 `skills/custom`）；`_scan` 只扫一层（`{workspace_id}` 会被静默跳过）；共用共识集合会把「删项目级」推演成「用户级也删了」，而 D3 的恢复能力正是靠两份集合互不干扰。
- **索引「路径」行用真实目录名，显示名用 frontmatter `name`**：两者不一致时（人工改过 frontmatter）旧实现会写出一个不存在的目录，模型读它得到的是「空的 Skill」而不是报错。沙箱脚本 `sync_overview.py` 与服务端 `render_overview()` 同步修改，仍然逐字节同构。
- **并发与冲突**：per-user `asyncio.Lock` 串行化「比对 + 写盘 + 更新 state」（临界区毫秒级，**进程内锁，多副本部署退化为 LWW**）；基线按 `(user, sandbox)` 分键，才能发现「两边各改一版」；覆盖前把本地版本整份快照到 `.conflicts/<ts>/`，不静默丢内容。
- **skill-creator 三脚本只依赖 Python 标准库**（`SKILL.md:107` 明确），在沙箱内运行；默认用户 Skill 根目录 `/home/user/skills/custom`（`init_skill.py:72`、`sync_overview.py:58`），与 `SANDBOX_SKILLS_PATH`（`storage/base.py:42`）一致。

### ② 配置项

| 环境变量 | 默认值 | 作用 | 读取位置 |
| --- | --- | --- | --- |
| `EDITH_USER_SKILLS_BACKEND` | `local` | 用户 Skill 存储后端：`local` \| `e2b`；未知值回退 `local` 并告警 | `main.py:168`、`storage/__init__.py:40` |
| `EDITH_USER_SKILLS_DIR` | `user-skills`（相对后端进程工作目录） | local 后端的权威副本根目录；已加入 `.gitignore` | `storage/local.py` |
| `EDITH_PROJECT_SKILLS_DIR` | `project-skills`（相对后端进程工作目录） | 项目级后端的权威副本根目录（与 `user-skills/` 平级，**不得嵌套**）；已加入 `.gitignore` | `storage/project.py` |

`e2b` 后端需要账号开通 E2B Volumes（未开通时 `POST /volumes` 返回 `403 use of volumes is not enabled`），此时应保持 `local`。`EDITH_E2B_TEMPLATE` 由 **sandbox** 模块读取（`main.py`），与本模块无直接关系。

### ③ 异常处理

| 场景 | 异常类型 | 处理策略 |
| --- | --- | --- |
| `userId` 缺失/空白 | `EDITHError(400, "invalid_request", ...)`（`http.py:26`） | 由 `http_utils.register_error_handler` 统一转为 `{"type","message"}` JSON，与 Go 版 `httpx.WriteError` 一致 |
| SKILL.md frontmatter 格式错误 / 缺 name、description | `ValueError`（`catalog.py:31,35,46`） | `load_catalog` 在 `Catalog` 构造时执行，异常向上传播——任何内置 Skill 格式错误都会阻止应用启动（对齐 Go 版「任何 Skill 格式错误都会阻止服务启动」；属预期失败而非降级） |
| local 后端目录不可读写 | `OSError` / `ValueError` | `read_index` 捕获后返回 `status=unavailable` + `reason`；**不再返回空字符串**（旧实现把故障压成 `""`，等于故障长期隐形） |
| E2B 卷未创建 | — | `status=empty, reason="user volume not created"`——这是「该用户没有自定义 Skill」，不是故障 |
| E2B 连接 / 读文件失败 | `Exception`（broad catch） | `status=unavailable` + 具体原因（`connect volume failed: ...` 等），与 `empty` 严格区分 |
| 同步失败（push/pull） | `Exception`（broad catch） | 返回 `SkillSyncResult(status=unavailable, reason=...)`；**不阻塞工具调用**，但必然出现在门面单行日志与 trace 中 |
| 脚本运行错误（目录已存在、不支持的资源目录、校验失败） | `OSError` / `ValueError` | 脚本捕获后打印中文错误到 stderr 并以退出码 1 结束（`init_skill.py:96-98`、`sync_overview.py:71-73`） |

### ④ 扩展点

- **新增公共 Skill**：在 `system/<skill-name>/` 下新建 `SKILL.md`（含 name+description frontmatter）与可选 `edith.yaml`，重启服务后自动进入目录（`load_catalog` 扫描，无注册表）。**改了内容需重建 E2B 模板**，否则沙箱内仍是旧正文。
- **新增用户 Skill 能力**：Agent 在沙箱内用 Sandbox 文件工具创建 `skills/custom/<name>/SKILL.md` 与 `edith.yaml`，索引会在下次读取时自动自愈，无需手动运行 `sync_overview.py`（流程见 `skill-creator/SKILL.md`）。
- **切换存储后端**：设置 `EDITH_USER_SKILLS_BACKEND=local|e2b` 即可，Agent 核心逻辑与 HTTP 契约不变。
- **新增存储后端**：实现 `SkillStorageBackend`（至少 `mount_spec` + `read_index`）并在 `new_skill_backend` 注册分支；挂载型后端无需实现 `push`/`pull`。
- **扩展 Skill 元数据**：在 `types.py` 的 `Skill` dataclass 与 `catalog.py:_load_skill` 中增加解析字段（如 `edith.yaml` 新键）。
- **新增 HTTP 能力**：在 `http.py:_build_router` 中追加路由（当前模块保持只读原则，新增写操作需同步审视职责边界）。

### 与 Go 版的差异点（代码核对）

| 差异 | Go 版 | Python 版 |
| --- | --- | --- |
| 内置 Skill 文件来源 | `embed.FS` 编译期内嵌（`module.go:11`） | 源码树 `system/` 运行期读取（`catalog.py:23`），并由沙箱模板构建时 COPY 进镜像 |
| 用户 Skill 存储 | E2B Volume（`volume.Service`）单一实现 | `SkillStorageBackend` 抽象，`local`（默认）/ `e2b` 可选 |
| 重复 Skill name 检测 | `loadCatalog` 检测重复名并报错（`catalog.go:90-93`） | 确认无（`load_catalog` 只排序不去重） |
| 用户摘要读取失败映射 | http.go 映射 500 `skills_load_failed`（`http.go:30-31`） | 归一为 `customStatus=unavailable` + `customReason`，HTTP 仍 200（故障对前端可见但不阻断列表） |

> 注：本文行号随代码变动可能再次偏移，定位时以符号名为准。
