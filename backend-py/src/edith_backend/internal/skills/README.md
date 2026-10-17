# skills（技能目录与 HTTP）

## 第一部分：模块名称与简介

**模块名**：`skills`

**一句话定位**：加载公共（内置）Skill 目录、读取当前用户 Volume 中由脚本生成的自定义 Skill 摘要（`overview.md`），并通过只读 HTTP 接口把两者提供给浏览器与 AgentRun。

**对应 Go 版源码路径**：`backend-v2/internal/skills/`（`catalog.go`、`http.go`、`module.go`、`types.go`，以及 `system/` 下的内置 Skill 文件）。

**文件清单**：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 模块出口：导出 `HTTP`、`Catalog`、`Skill`、`SkillSummary`、`SkillListItem`、`SkillsResponse`、`load_catalog`、`parse_user_overview` |
| `catalog.py` | 内置 Skill 加载（扫描 `system/`）与用户 `overview.md` 解析；`Catalog` 目录服务类 |
| `http.py` | HTTP 边界：`GET /internal/skills` 只读列表接口 |
| `types.py` | 类型定义（`Skill`、`SkillSummary` 为 dataclass；`SkillListItem`、`SkillsResponse` 为 Pydantic 模型）与 Sandbox 相对路径常量 `SYSTEM_PATH`/`CUSTOM_PATH` |
| `system/current-time/SKILL.md`、`edith.yaml` | 内置公共 Skill：需要当前时间时调用 `get_current_time` 工具 |
| `system/skill-creator/SKILL.md`、`edith.yaml` | 内置公共 Skill：按 EDITH 规范创建/维护用户 Skill 的工作方法 |
| `system/skill-creator/scripts/init_skill.py` | 创建用户 Skill 骨架（`SKILL.md` + `edith.yaml` + 可选资源目录），仅用 Python 标准库 |
| `system/skill-creator/scripts/quick_validate.py` | 校验一个 Skill 目录的格式（frontmatter、name/description 约束、edith.yaml），仅用 Python 标准库 |
| `system/skill-creator/scripts/sync_overview.py` | 扫描用户 Skills 目录并生成 `overview.md` 摘要索引，仅用 Python 标准库 |

## 第二部分：架构设计

### ① 分层

| 层次 | 载体 | 说明 |
| --- | --- | --- |
| HTTP 边界层 | `http.py` 的 `HTTP` 类 | 持有 FastAPI `APIRouter`，只暴露 `GET /internal/skills` 只读接口；校验 `userId`，组装响应模型 |
| 服务/目录层 | `catalog.py` 的 `Catalog` 类 + 模块级函数 `load_catalog` / `parse_user_overview` | 内置 Skill 的内存目录与用户摘要解析；对上层屏蔽文件与 Volume 细节 |
| 数据访问层 | 注入的 `volume.Service`（外部模块） | `Catalog` 不直接碰 DB / E2B，通过构造器注入的 `volume.Service.read_user_overview` 读取远端 Volume 根目录 `/overview.md` |
| 类型定义层 | `types.py` | dataclass（内部）与 Pydantic（HTTP 契约）分离；路径常量 `SYSTEM_PATH = "skills/system"`、`CUSTOM_PATH = "skills/custom"` |

### ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造器注入） | `catalog.py:92`（`Catalog.__init__(volumes=None)`）、`http.py:16`（`HTTP.__init__(catalog)`） | 组合根 `main.py:69` `skills = SkillsCatalog(volumes)` 显式装配；`Catalog` 对 `volumes` 为可选依赖（`None` 时用户摘要返回空） |
| 工厂 | `http.py:20` `HTTP._build_router()` | 在构造时构建并返回 `APIRouter`，路由闭包捕获 `self.catalog` |
| 适配器 | `catalog.py:68` `parse_user_overview` | 把 `volume.Service` 返回的 `overview.md` 原始文本适配为 `list[SkillSummary]`（解析脚本生成的 `- \`name\`：description` 行） |
| 策略（简单排序） | `catalog.py:64`、`catalog.py:85` | 内置 Skill 与用户摘要均按 `name` 排序，保证 `list_system_summaries`/`parse_user_overview` 输出稳定 |

### ③ 关键类/函数

| 名称 | 所在文件 | 职责/关键签名 |
| --- | --- | --- |
| `Catalog` | `catalog.py:89` | 目录服务：`__init__(volumes=None)`；`list_system_summaries() -> list[SkillSummary]`（内存，稳定顺序）；`async list_user_summaries(user_id) -> list[SkillSummary]`（读 overview 并解析）；`async read_user_overview(user_id) -> str`（原文透传） |
| `load_catalog()` | `catalog.py:57` | 扫描 `_SYSTEM_DIR`（`catalog.py:14`，本文件旁 `system/`），逐个加载含 `SKILL.md` 的目录，按 `name` 排序返回 `list[Skill]` |
| `_load_skill(skill_dir)` | `catalog.py:29` | 解析单个 Skill：`SKILL.md` frontmatter（必填 `name`/`description`）+ 可选 `edith.yaml`（`display_name`/`short_description` 回退到 name/description） |
| `_split_frontmatter(content)` | `catalog.py:17` | 分离 SKILL.md 的 YAML 头部与正文，兼容 BOM（`\uFEFF`）；格式错误抛 `ValueError` |
| `parse_user_overview(content)` | `catalog.py:68` | 解析 `sync_overview.py` 生成的摘要行（`- \`name\`：desc`），按 `name` 排序 |
| `HTTP` | `http.py:15` | HTTP 边界：`__init__(catalog)`；`_build_router() -> APIRouter`；路由 `GET /internal/skills`（query 参数 `userId`，必填） |
| `Skill` / `SkillSummary` | `types.py:17` / `types.py:29` | dataclass：解析完成的 Skill（含 `body`/`display_name`/`short_description`）/ 传给 AgentRun 的最小摘要 |
| `SkillListItem` / `SkillsResponse` | `types.py:37` / `types.py:44` | Pydantic 模型：浏览器展示的摘要项 / `{system, custom}` 列表 HTTP 输出 |
| `SYSTEM_PATH` / `CUSTOM_PATH` | `types.py:13-14` | 常量：Sandbox 文件工具使用的公共/用户 Skills 相对路径 |
| `create_skill(name, root, resources, display_name, short_description)` | `init_skill.py:36` | 创建 Skill 骨架（`SKILL.md`、`edith.yaml`、`scripts`/`references`/`assets` 资源目录）；目录已存在抛 `ValueError` |
| `validate_skill(skill_path)` | `quick_validate.py:48` | 校验 Skill 目录，返回 `(是否通过, 中文消息)`；覆盖 frontmatter 完整性、name 命名规则（小写字母/数字/单连字符）、目录名与 name 一致、长度上限、`<`/`>` 字符、edith.yaml 键约束 |
| `build_overview(root)` | `sync_overview.py:31` | 扫描 custom 目录，为每个 Skill 生成 `- \`name\`：description` 与路径行，输出 `overview.md` 文本 |

## 第三部分：与其他模块的联系

### ① 依赖关系

**本模块 import 的内部模块/外部库**：

| 依赖 | 用途 |
| --- | --- |
| `..http_utils.EDITHError`（`http.py:10`） | 抛出统一业务错误（status + type + message 三元组），由 `http_utils.register_error_handler` 转为 `{"type","message"}` JSON |
| `volume.Service`（构造器注入，`main.py:69`） | 通过 `read_user_overview(user_id)` 读取用户 E2B Volume 根目录 `/overview.md`（`volume/types.py:13` `USER_OVERVIEW_PATH`）；本模块不直接依赖 E2B SDK / DB |
| `PyYAML`（`catalog.py:10`，`yaml.safe_load`） | 解析 `SKILL.md` frontmatter 与 `edith.yaml` |
| `fastapi.APIRouter`、`pydantic.BaseModel` | HTTP 路由与响应模型 |

**谁依赖本模块**：

- 组合根 `main.py`：`from .internal.skills import HTTP as SkillsHTTP, Catalog as SkillsCatalog`（`main.py:35-36`）；`skills = SkillsCatalog(volumes)`（`main.py:69`）；注入 `Dependencies(skills=skills, ...)` 给 agentrun（`main.py:91`）；`app.include_router(SkillsHTTP(skills).router)`（`main.py:108`）
- `agentrun/configuration.py`：`RunConfigurations` 持有 `skills: SkillsCatalog | None`（`configuration.py:12,44`），运行时调用 `list_system_summaries()` 与 `await read_user_overview(user_id)`（`configuration.py:95-96`）；并 import `skills.types` 的 `SYSTEM_PATH`/`CUSTOM_PATH`/`SkillSummary`（`configuration.py:13`）

### ② 数据流向

**典型业务流转 A：浏览器拉取 Skills 列表**

```text
Web BFF（Next.js）
  └─ GET /internal/skills?userId=xxx            （http.py:24）
       ├─ 校验 userId：为空 → EDITHError(400, "invalid_request", "userId is required")
       ├─ catalog.list_system_summaries()       （内存，内置 Skill 摘要）
       └─ await catalog.list_user_summaries(userId)
            └─ await volumes.read_user_overview(user_id)   （volume.Service）
                 └─ 连接用户 E2B Volume，读根目录 /overview.md（不存在/失败 → 返回 ""）
       └─ parse_user_overview(...) → SkillsResponse{system, custom} → JSON
```

**典型业务流转 B：AgentRun 一次运行的指令注入**

```text
agentrun.RunConfigurations.load(request)              （configuration.py:57）
  ├─ skills.list_system_summaries()                   （configuration.py:95）
  ├─ await skills.read_user_overview(request.user_id) （configuration.py:96）
  └─ skill_instruction(summaries, overview, uploads)  （configuration.py:152-170）
       ├─ 拼"可用公共 Skills：- name：description"
       ├─ 拼"可用用户 Skills：" + overview 原文
       └─ 注明完整正文位置：公共 skills/system/<skill-name>/、用户 skills/custom/<skill-name>/（Sandbox 工作区相对路径）
```

完整 Skill 正文**不**进入指令：Agent 在 E2B 沙箱内通过 Sandbox 文件工具按需读取（公共 `skills/system/` 在模板内；用户 Volume 由 sandbox 模块经 `volume.Service.mount_for_user` 挂载到 `/home/user/skills/custom`，`volume/types.py:11`）。沙箱侧 `skills/system/` 目录的部署方式未在本仓库代码中发现（待确认）。

### ③ 接口调用

**对外 HTTP 路由**：

| Method | Path | 参数 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| `GET` | `/internal/skills` | query `userId`（必填） | `SkillsResponse`：`{ "system": [{name, description}], "custom": [...] }` | 只读列表；`userId` 缺失返回 400 `invalid_request` |

- 注册点：`main.py:108` `app.include_router(SkillsHTTP(skills).router)`（挂在整个 FastAPI 应用上，路径前缀即 `/internal/skills`）
- 调用方：Web BFF（Next.js）转发 `GET /api/skills` → 后端 `GET /internal/skills`（对齐 Go 版 README 的说明）

**对外内部方法（`Catalog`）**：

| 方法 | 签名 | 调用方 |
| --- | --- | --- |
| `list_system_summaries` | `() -> list[SkillSummary]` | `http.py:29`、`configuration.py:95` |
| `list_user_summaries` | `async (user_id: str) -> list[SkillSummary]` | `http.py:27` |
| `read_user_overview` | `async (user_id: str) -> str` | `configuration.py:96` |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

- **公共 Skills 启动时全量加载**：`Catalog` 构造时 `load_catalog()` 从源码树 `system/`（`catalog.py:14`）一次性解析全部含 `SKILL.md` 的目录到内存，之后只读；Go 版用 `embed.FS` 内嵌，Python 版直接读源码目录（运行期依赖安装包携带这些数据文件）。
- **SKILL.md 契约**：必须以 `---` 开头、frontmatter 至少含 `name` 与 `description`（缺失抛 `ValueError`）；`edith.yaml` 可选，仅提供 `display_name`/`short_description` 展示字段，回退到 name/description（`catalog.py:36-45`）。
- **摘要与正文分离（渐进加载）**：指令只注入摘要；完整正文由 Agent 通过 Sandbox 文件工具按需读取，避免把大段 Skill 正文无条件塞进每次请求。
- **用户 Skills 只读索引**：用户摘要只来自 Volume 根目录 `/overview.md`（由 `sync_overview.py` 生成），只解析 `- \`name\`：description` 行；不扫描 Volume、不创建 Volume、不读完整正文（对齐 Go 版语义）。
- **overview.md 为派生索引**：由 `sync_overview.py` 生成，`parse_user_overview` 的解析格式与该脚本输出是隐式契约（标题、路径行等 Markdown 内容不会被当作 Skill 项）。
- **skill-creator 三脚本只依赖 Python 标准库**（`SKILL.md:107` 明确），在 E2B 沙箱内运行；默认用户 Skill 根目录 `/home/user/skills/custom`（`init_skill.py:72`、`sync_overview.py:56`），与 `volume/types.py:11` 的挂载路径一致。

### ② 配置项

**无**：skills 模块自身不读取任何环境变量/配置（未发现）。相关的 `EDITH_E2B_TEMPLATE` 由 **sandbox** 模块读取（`main.py:66` 传默认 `"default"`；`config.py:51-56` 中 `sandbox_template()` 将其视为必填并抛 `RuntimeError`），与本模块无直接关系。

### ③ 异常处理

| 场景 | 异常类型 | 处理策略 |
| --- | --- | --- |
| `userId` 缺失/空白 | `EDITHError(400, "invalid_request", ...)`（`http.py:26`） | 由 `http_utils.register_error_handler`（`http_utils.py:30-35`，`main.py:130` 注册）统一转为 `{"type","message"}` JSON，与 Go 版 `httpx.WriteError` 一致 |
| SKILL.md frontmatter 格式错误 / 缺 name、description | `ValueError`（`catalog.py:22,26,37`） | `load_catalog` 在 `Catalog` 构造时执行，异常向上传播——任何内置 Skill 格式错误都会阻止应用启动（对齐 Go 版 module.go 注释"任何 Skill 格式错误都会阻止服务启动"；Python 版未发现显式 try/except 包装，属预期失败而非降级） |
| 用户 Volume 不存在 / `/overview.md` 不存在 / E2B 不可用 | `Exception`（broad catch） | **降级**：在 `volume.Service.read_user_overview` 内部捕获并记 warning 日志，返回 `""`（`volume/service.py:49-60`），`list_user_summaries` 因此正常返回空列表，Agent 继续运行 |
| `list_user_summaries` 其他意外异常 | — | Go 版 http.go 映射为 500 `skills_load_failed`；Python 版 `http.py` 未发现对应捕获分支（volume 层已降级为空，该路径实际难触发），待确认 |
| 脚本运行错误（目录已存在、不支持的资源目录、校验失败、目录不存在） | `OSError` / `ValueError` | 脚本捕获后打印中文错误到 stderr 并以退出码 1 结束（`init_skill.py:96-98`、`sync_overview.py:71-73`） |

### ④ 扩展点

- **新增公共 Skill**：在 `system/<skill-name>/` 下新建 `SKILL.md`（含 name+description frontmatter）与可选 `edith.yaml`，重启服务后自动进入目录（`load_catalog` 扫描，无注册表）。
- **新增用户 Skill 能力**：Agent 在沙箱内用 Sandbox 文件工具创建 `skills/custom/<name>/SKILL.md` 与 `edith.yaml`，运行 `sync_overview.py` 更新摘要索引（流程见 `skill-creator/SKILL.md` 的"创建流程"）。
- **扩展 Skill 元数据**：在 `types.py` 的 `Skill` dataclass 与 `catalog.py:_load_skill` 中增加解析字段（如 `edith.yaml` 新键）。
- **新增 HTTP 能力**：在 `http.py:_build_router` 中追加路由（当前模块保持只读原则，新增写操作需同步审视与 Go 版的职责对齐）。
- **增强脚本工具链**：修改 `system/skill-creator/scripts/` 后需同步到 E2B 模板内对应路径（沙箱内 `skills/system/skill-creator/scripts/`，部署机制不在本仓库，待确认），并保持 `quick_validate.py` 的约束与 `sync_overview.py` 输出格式的兼容。
- **更换/新增存储**：用户摘要读取统一走注入的 `volumes`（`Catalog._volumes`，`None` 时降级为空），可替换为其他实现而不改 HTTP/AgentRun 调用方。

### 与 Go 版的差异点（代码核对）

| 差异 | Go 版 | Python 版 |
| --- | --- | --- |
| 内置 Skill 文件来源 | `embed.FS` 编译期内嵌（`module.go:11`） | 源码树 `system/` 运行期读取（`catalog.py:14`） |
| 重复 Skill name 检测 | `loadCatalog` 检测重复名并报错（`catalog.go:90-93`） | `load_catalog` 未发现重复名检测，待确认 |
| 用户摘要读取失败映射 | http.go 映射 500 `skills_load_failed`（`http.go:30-31`） | volume 层已降级为空，http.py 无此分支（见"异常处理"） |
