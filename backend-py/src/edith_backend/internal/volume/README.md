# 第一部分：模块名称与简介

## 模块名

`volume`（源码路径：`src/edith_backend/internal/volume/`）

## 一句话定位

把 Clerk 用户绑定到唯一的 E2B 持久化 Volume，为 Sandbox 提供用户自定义 Skills 挂载信息、为 Skills 模块提供用户 overview 摘要读取的**内部能力模块**（不对外注册 HTTP 路由）。

## 对应 Go 版源码路径

`backend-v2/internal/volume/`（`module.go` / `service.go` / `store.go` / `trace.go` / `types.go`）。本模块是该目录的 Python 重写版，职责一一对应。

## 文件清单

| 文件 | 一句话职责 |
| --- | --- |
| `__init__.py` | 模块公开导出：`Mount`、`Service`、`Store`、`UserVolume`、`create_schema` |
| `service.py` | `Service` 服务类：用户↔Volume 绑定核心逻辑（读 overview、取挂载信息、连接 Volume、懒创建） |
| `store.py` | `user_volumes` 表结构与 SQLite 访问器（`load` / `save` / `update_token`） |
| `trace.py` | 诊断开关 `EDITH_E2B_DEBUG_HTTP` 与隐私指纹函数（userId / token 的 sha256 指纹化） |
| `types.py` | 路径常量（`CUSTOM_SKILLS_PATH`、`USER_OVERVIEW_PATH`）与数据类（`UserVolume`、`Mount`） |

---

# 第二部分：架构设计

## ① 分层

本模块**没有 HTTP 边界层**（不注册任何路由，是纯内部能力），按职责分为以下四层：

| 层 | 文件 | 说明 |
| --- | --- | --- |
| 类型定义层 | `types.py` | 路径常量与 `UserVolume`、`Mount` 两个 dataclass，作为服务层与调用方之间的数据契约 |
| 数据访问层 | `store.py` | `Store` 封装对 SQLite `user_volumes` 表的全部访问；`create_schema` 负责建表 |
| 服务层 | `service.py` | `Service` 编排 Store 与 e2b SDK：懒创建、连接、挂载信息、overview 读取，并对 E2B 异常做降级 |
| 诊断辅助（横切） | `trace.py` | 提供日志开关与指纹函数，供服务层日志脱敏使用 |

分层依赖方向：`service.py` → `store.py` / `trace.py` / `types.py`；`store.py` 只依赖 `aiosqlite`。模块外部（sandbox、skills、main.py）只通过 `__init__.py` 的公开导出访问。

## ② 设计模式

| 模式 | 代码位置 | 说明 |
| --- | --- | --- |
| 依赖注入（构造注入） | `service.py:24-26`（`Service.__init__(store)`）；`main.py:65`（`VolumeService(VolumeStore(db))`）；`sandbox/service.py:28`、`skills/catalog.py:92` 注入 `VolumeService` | 服务只依赖 `Store` 接口与 e2b SDK，不自行创建依赖；组合根 `main.py` 显式装配 |
| 仓储（Repository） | `store.py:25-53` | `Store` 把 `user_volumes` 表的 SQL 全部收拢，屏蔽底层 aiosqlite 细节 |
| 适配器（门面） | `service.py:11,50,99` | 把 E2B SDK 的 `AsyncVolume` 能力适配为「按用户」的领域接口（`mount_for_user` / `read_user_overview` / `open_for_user`） |
| 惰性创建 + 互斥锁 | `service.py:92-106`（`_get_or_create` + `asyncio.Lock`） | 首次访问才创建远端 Volume；单实例下串行化首次创建，避免远端出现两个同用户 Volume |
| 降级（Graceful Degradation） | `service.py:53-60,73-79` | E2B 不可用/未启用 Volumes 时返回空串或 `None`，让上层继续运行 |
| 工厂方法（SDK 侧） | `service.py:99,50` | 调用 `AsyncVolume.create(name)` / `AsyncVolume.connect(volume_id)` 创建/连接远端资源 |

## ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `Service` | `service.py:21` | 核心服务类；构造时注入 `Store`，内部持有 `asyncio.Lock` |
| `Service.read_user_overview` | `service.py:28` | `async def read_user_overview(self, user_id: str) -> str`：读取用户 Volume 中 `/overview.md`；Volume 不存在/文件不存在/E2B 不可用时返回空串，不创建远端 Volume |
| `Service.mount_for_user` | `service.py:62` | `async def mount_for_user(self, user_id: str) -> Mount \| None`：返回用户 Skills Volume 挂载信息，首次调用会创建远端 Volume；E2B 未启用 Volumes 时降级返回 `None` |
| `Service.open_for_user` | `service.py:82` | `async def open_for_user(self, user_id: str) -> AsyncVolume`：连接当前用户的 Volume；映射不存在时抛 `ValueError("user volume not found")` |
| `Service._get_or_create` | `service.py:92` | 私有方法：锁内查库，未命中则 `AsyncVolume.create("edith-user-volume-" + user_id)` 并持久化；创建结果不完整时 `destroy()` 清理并抛 `ValueError` |
| `Store` | `store.py:25` | SQLite 访问器，构造时注入 `aiosqlite.Connection` |
| `Store.load` | `store.py:29` | `async def load(self, user_id: str) -> tuple[str, str, str] \| None`：返回 `(volume_id, volume_name, volume_token)` 或 `None` |
| `Store.save` | `store.py:41` | `async def save(self, user_id, volume_id, volume_name, token) -> None`：插入用户↔Volume 映射 |
| `Store.update_token` | `store.py:48` | `async def update_token(self, user_id, token) -> None`：更新 token 并刷新 `updated_at`（当前 Python 版无调用方，见第四部分） |
| `create_schema` | `store.py:11` | `async def create_schema(db: aiosqlite.Connection) -> None`：建 `user_volumes` 表（幂等） |
| `trace_enabled` | `trace.py:15` | `-> bool`：`EDITH_E2B_DEBUG_HTTP == "true"`（忽略大小写与空白）时开启诊断日志 |
| `user_fingerprint` | `trace.py:19` | `-> str`：userId 的 sha256 十六进制前 12 位，避免日志泄露明文 userId |
| `token_fingerprint` | `trace.py:24` | `-> str`：token 的完整 sha256 十六进制，避免日志泄露明文密钥 |
| `UserVolume` | `types.py:17` | dataclass：`id`、`name`；不含 token（对齐 Go 版注释：避免 token 被带到 HTTP 输出） |
| `Mount` | `types.py:25` | dataclass：`name`、`path`；Sandbox 创建时的 Volume 挂载信息 |
| `CUSTOM_SKILLS_PATH` | `types.py:11` | 常量 `/home/user/skills/custom`：用户 Volume 在 Sandbox 中的固定挂载路径 |
| `USER_OVERVIEW_PATH` | `types.py:13` | 常量 `/overview.md`：用户 Volume 中 Skills 摘要索引路径 |

---

# 第三部分：与其他模块的联系

## ① 依赖关系

**本模块 import 的内部模块 / 外部 SDK：**

| 依赖 | 用途 |
| --- | --- |
| `e2b.AsyncVolume`（外部 SDK） | 远端 E2B Volume 的创建（`create`）、连接（`connect`）、读文件（`read_file`）、销毁（`destroy`） |
| `aiosqlite`（外部） | `store.py` 访问 SQLite；连接对象由 `main.py` 打开后经 `Store` 注入 |
| `.store`（`Store`、`create_schema`） | 数据访问层 |
| `.trace`（`trace_enabled`、`user_fingerprint`、`token_fingerprint`） | 日志开关与隐私指纹 |
| `.types`（`CUSTOM_SKILLS_PATH`、`USER_OVERVIEW_PATH`、`Mount`、`UserVolume`） | 常量与数据契约 |
| DB 表 `user_volumes` | 持久化用户↔Volume 映射（含 token） |

**被谁依赖（反向）：**

| 调用方 | 位置 | 用途 |
| --- | --- | --- |
| sandbox 模块 | `sandbox/service.py:13-14,28,70` | `workspace()` 创建 Sandbox 时调用 `mount_for_user` 获取 `volume_mounts`；`main.py` 注入 `VolumeService` |
| skills 模块 | `skills/catalog.py:92,102-111` | `list_user_summaries` / `read_user_overview` 调用 `read_user_overview` 读取用户自定义 Skills 摘要 |
| 组合根 | `main.py:55,65-66,69` | `create_volume_schema(db)` 建表；`VolumeService(VolumeStore(db))` 装配；注入 `SandboxService` 与 `SkillsCatalog` |

## ② 数据流向

**场景一：用户首次请求 Sandbox（Volume 懒创建）**

```
sandbox.Service.workspace(user_id, session_id)
  └─► volumes.mount_for_user(user_id)            [volume 模块入口]
        ├─► Store.load(user_id) → None（未命中）
        ├─► AsyncVolume.create("edith-user-volume-{user_id}")   （远端 E2B 资源）
        ├─► Store.save(user_id, volume_id, name, token)          （持久化映射）
        └─► 返回 Mount(name, "/home/user/skills/custom")
  └─► AsyncSandbox.create(..., volume_mounts={mount.name: CUSTOM_SKILLS_PATH})
```

**场景二：用户再次请求（命中映射）**

```
mount_for_user(user_id) → Store.load 命中 → 直接返回 Mount（不再触碰远端创建接口）
```

**场景三：Skills 模块读取用户 overview**

```
skills.Catalog.list_user_summaries(user_id)
  └─► volumes.read_user_overview(user_id)        [volume 模块入口]
        ├─► Store.load(user_id) → None 则返回 ""
        ├─► AsyncVolume.connect(volume_id)
        ├─► volume.read_file("/overview.md")
        └─► 返回文本（strip 后）或 ""（任何 E2B 异常降级）
  └─► parse_user_overview(overview) → list[SkillSummary]
```

数据进出本模块的边界：**进**——`user_id`（字符串）；**出**——`Mount`（name+path）、`str`（overview 文本）、`AsyncVolume`（仅 `open_for_user`）；持久化映射经 `Store` 写 `user_volumes` 表。

## ③ 接口调用

**HTTP 路由：无。** 本模块不注册任何路由（对齐 Go 版 `service.go` 注释「不注册 HTTP 路由」），`main.py` 也未 `include_router` volume 的路由。

**暴露的内部方法：**

| 方法 | 被谁调用 | 调用位置 |
| --- | --- | --- |
| `read_user_overview(user_id)` | skills.Catalog | `skills/catalog.py:104,111` |
| `mount_for_user(user_id)` | sandbox.Service | `sandbox/service.py:70`（`workspace()` 创建 Sandbox 时） |
| `open_for_user(user_id)` | **未发现调用方** | 仅在 `service.py:82` 定义；对齐 Go 版 `OpenForUser`「供需要读写完整用户文件的内部能力使用」的预留接口 |

---

# 第四部分：模块内容扩展说明

## ① 核心业务逻辑要点

1. **用户↔Volume 一对一映射**：以 `user_id` 为主键持久化在 `user_volumes` 表，映射关系为「一个用户一个 E2B Volume」；volume_id、volume_name 均 UNIQUE。
2. **懒创建 + 串行保护**：`_get_or_create` 在 `asyncio.Lock` 内先查库，未命中才 `AsyncVolume.create`；创建结果（volume_id/name/token）不完整时调用 `created.destroy()` 清理并抛 `ValueError`，避免留下半成品远端资源。
3. **降级策略**：
   - `mount_for_user`：任何异常（如 403 volumes not enabled）→ `logger.warning` → 返回 `None`，Sandbox 仍可创建，只是用户自定义 Skills 不可用（`sandbox/service.py:76-78` 相应传 `volume_mounts=None`）。
   - `read_user_overview`：Volume 不存在、文件不存在或 E2B 不可用 → 返回 `""`，让 Agent 继续运行；**不会**创建远端 Volume。
4. **隐私脱敏**：日志中 userId 只记 `user_fingerprint`（sha256 前 12 位）；token 只记 `token_sha256` 前缀与 `token_len`，不落明文。
5. **固定路径约定**：挂载路径 `CUSTOM_SKILLS_PATH = /home/user/skills/custom`，overview 索引 `USER_OVERVIEW_PATH = /overview.md`（由 `sync_overview.py` 等外部脚本写入，见 `skills/catalog.py:69` 注释）。
6. **命名规则**：Volume 名 = `"edith-user-volume-" + user_id`（Python 版；Go 版用 `uuid.NewString()`，见下）。
7. **与 Go 版值得注意的差异（Python 以本代码为准）**：
   - Go `OpenForUser` 会**懒创建** Volume 并经控制面刷新 token（`loadCurrentToken`），Python `open_for_user` 仅查库、未命中直接抛 `ValueError("user volume not found")`，且无 token 刷新逻辑——该差异对当前无调用方无影响，但未来若启用 `open_for_user` 需补齐（待确认是否有意为之）。
   - Python `read_user_overview` 的 `AsyncVolume.connect(volume_id)` 仅传 volume_id（token 由 SDK 侧处理），而 Go 显式传 token 并做 token 变更回写（`store.updateToken`）——Python 版 `Store.update_token` 因此暂无调用方。
   - Go 的 trace 在 `EDITH_E2B_DEBUG_HTTP=true` 时挂载 HTTP transport 级 tracer（`volumeTraceTransport`，记录 method/host/path/状态码等）；Python `trace.py` 目前只提供开关与指纹函数，**HTTP transport 级 tracer 未实现**，仅用于 service.py 内日志开关。

## ② 配置项

本模块直接读取的环境变量：

| 环境变量 | 位置 | 说明 |
| --- | --- | --- |
| `EDITH_E2B_DEBUG_HTTP` | `trace.py:12,16` | 设为 `"true"`（忽略大小写/空白）时开启 E2B Volume 边界的短时诊断日志 |

**未读取**：`EDITH_E2B_TEMPLATE`（由 `main.py:66` 读取并注入 sandbox 模块，与本模块无关）；E2B API 凭据（如 `E2B_API_KEY` 等）由 e2b SDK 内部处理，本模块代码中未显式读取（具体变量名待确认）。数据库连接由 `main.py` 的 `config.database_path()` 统一打开后注入。

## ③ 异常处理

| 异常来源 | 处理策略 | 代码位置 |
| --- | --- | --- |
| 空 `user_id`（三个公开方法） | 抛 `ValueError("volume requires a user ID")`（参数校验，不降级） | `service.py:35-36,69-70,85-86` |
| `read_user_overview` 中 E2B 任意异常（连接/读文件） | `logger.warning`（指纹化日志）→ 返回 `""`（降级，Agent 继续运行） | `service.py:53-60` |
| `mount_for_user` 中 E2B 任意异常（含 403 volumes not enabled） | `logger.warning` → 返回 `None`（降级，Sandbox 无卷继续创建） | `service.py:73-79` |
| `_get_or_create` 创建结果不完整 | `created.destroy()` 清理 → 抛 `ValueError("create user volume returned incomplete information")` | `service.py:100-102` |
| `_get_or_create` 中 `AsyncVolume.create` 抛错 | 不捕获，向上传播（由 `mount_for_user` 捕获降级） | `service.py:99` |
| `open_for_user` 映射不存在 | 抛 `ValueError("user volume not found")` | `service.py:88-89` |

日志策略：统一使用 `logging` 的 `logger.warning`；userId 经 `user_fingerprint` 脱敏，token 仅记指纹前缀与长度。本地数据库（aiosqlite）错误不降级，直接向上抛。

## ④ 扩展点

1. **新增读写用户完整文件的内部能力**：调用 `open_for_user(user_id)` 获取 `AsyncVolume`；当前该方法无调用方且无懒创建/token 刷新，启用前需对照 Go 版补齐语义（`service.go` 的 `loadCurrentToken` / `getOrCreateUserVolume`）。
2. **新增挂载路径/卷用途**：在 `types.py` 增加路径常量，并在 `sandbox/service.py` 的 `volume_mounts` 中扩展（或新增服务方法返回新的 `Mount`）。
3. **更换存储后端**：保持 `Store.load/save/update_token` 接口不变，替换 `store.py` 实现即可（`Service` 只依赖该接口）。
4. **为模块增加 HTTP 路由**：目前 volume 是纯内部能力；如需对外暴露（如查询用户卷状态），可仿照其他模块新增 `http.py` 并在 `main.py` `include_router`。
5. **补全诊断能力**：对齐 Go 版 `volumeTraceTransport`，在 `EDITH_E2B_DEBUG_HTTP=true` 时挂载 E2B HTTP 请求/响应 tracer（当前 `trace.py` 仅有开关与指纹函数）。
6. **新增 provider**：如需支持 E2B 之外的卷后端，可在 `service.py` 之上抽象「VolumeProvider」接口（当前直接依赖 `e2b.AsyncVolume`，未做抽象）。
