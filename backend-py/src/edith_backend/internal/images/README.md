# images 模块（图片上传与存储（COS））

## 第一部分：模块名称与简介

**模块名**：`images`

**一句话定位**：管理聊天图片的元数据（SQLite 持久化）、腾讯云 COS 对象存储访问与 Agent 图片输入，对外提供「预留上传 → 客户端直传 COS → 确认完成 → 短期签名读取」的完整图片生命周期。

**对应 Go 版源码路径**：`EDITH/backend-v2/internal/images/`（Python 版为 `backend-py` 对 `backend-v2` 的逐文件重构，模块一一对应）。

**文件清单**（`src/edith_backend/internal/images/` 下）：

| 文件名 | 职责 |
| --- | --- |
| `__init__.py` | 模块公开导出（`HTTP` / `Image` / `Module` / `Service` / `Store` / `create_schema` / `new_module`） |
| `agent_input.py` | Agent 输入适配：把图片 ID 转为本次模型调用可访问的短期 URL；提供持久图片引用标记（`edith-image://` 前缀）的编解码 |
| `cos.py` | COS 客户端接口（`COSClient` Protocol）与腾讯云实现（`TencentCOS`）、配置对象（`COSConfig`）与签名有效期常量 |
| `http.py` | HTTP 边界：创建上传、完成上传、打开图片（302 跳转到签名 URL）三条路由及参数校验、错误映射 |
| `module.py` | 模块组装入口：`new_module(db, config)` 创建建表、Store、Service、AgentInput、HTTP 并装配为 `Module` |
| `service.py` | 服务层：图片预留（校验 + 落库 + 签发 PUT 签名）、上传确认（COS head 校验 + 置 ready）、读取（user/session 维度）、失败清理 |
| `store.py` | SQLite 数据访问层：`chat_images` 表的建表与 CRUD，`ImageRecord` 记录对象与 `ImageNotFound` 异常 |
| `types.py` | 类型定义：领域 dataclass（`Image` / `UploadInput`）与 Pydantic v2 请求/响应模型（camelCase 别名） |

## 第二部分：架构设计

### ① 分层

按代码实际分层，自外向内：

| 层次 | 文件 | 职责 |
| --- | --- | --- |
| 组装层 | `module.py` | 组合根 `new_module`：建表 → `Store` → `Service` → `AgentInput` / `HTTP`，对外暴露 `Module`（含 `agent_input` 与 `http` 两个能力） |
| HTTP 边界层 | `http.py` | FastAPI `APIRouter` 路由定义、请求参数校验、业务异常（`ValueError` / 其他）映射为 `EDITHError` |
| 服务层 | `service.py` | 业务规则：MIME/大小校验、两阶段状态流转（pending → ready）、COS 调用编排、失败回滚与清理 |
| 数据访问层 | `store.py` | 封装 `chat_images` 表的全部 SQL（insert / delete / mark_ready / for_user / for_session） |
| 客户端适配层 | `cos.py` | `COSClient` 接口抽象 + `TencentCOS` 腾讯云实现（cos-python-sdk-v5），支持无配置降级（不实例化客户端） |
| 类型定义层 | `types.py` | 领域类型（dataclass）与 API 契约（Pydantic v2 BaseModel，camelCase 别名） |
| Agent 输入适配层 | `agent_input.py` | 供 `agentrun` 模块调用：批量把图片 ID 解析为短期 URL；图片引用标记编解码 |

### ② 设计模式

| 模式 | 说明 | 代码位置 |
| --- | --- | --- |
| 工厂方法 | `Service.build(store, config)` 根据 `config.configured()` 决定是否实例化 `TencentCOS`（无配置则 `cos=None` 降级）；`new_module(db, config)` 组装整个模块 | `service.py:32-37`、`module.py:25-31` |
| 依赖注入 | 全部依赖通过构造函数显式注入：`Store(db)`、`Service(store, cos)`、`HTTP(service)`、`AgentInput(service)`；数据库连接 `db` 由组合根 `main.py` 传入 | `store.py:43`、`service.py:28`、`http.py:27`、`agent_input.py:14`、`module.py:20` |
| 适配器模式 | `COSClient`（`typing.Protocol`）抽象 `sign_put/sign_get/head/delete`，`TencentCOS` 为腾讯云适配器；接口化便于测试 mock 与无配置降级 | `cos.py:21-25`、`cos.py:43-72` |
| 仓储模式 | `Store` 封装 `chat_images` 表全部持久化操作，服务层不直接写 SQL | `store.py:42-91` |
| 状态机（两态） | 图片状态 `pending`（已预留）→ `ready`（已确认完成）；只有 `ready` 才能被读取 | `store.py:12-13`（`PENDING`/`READY`）、`service.py:66-81`（`complete_upload` 流转）、`store.py:59-61`（`mark_ready`） |
| 常量集中 | MIME 白名单、大小上限、签名有效期、对象键前缀集中定义 | `service.py:14-16`、`cos.py:12`、`service.py:53` |

### ③ 关键类 / 函数

| 名称 | 所在文件 | 职责 / 关键签名 |
| --- | --- | --- |
| `new_module(db, config)` | `module.py` | 模块工厂：`async def new_module(db: aiosqlite.Connection, config: COSConfig) -> Module`；`db` 为 None 时抛 `ValueError` |
| `Module` | `module.py` | 对外能力集合：`agent_input: AgentInput`、`http: HTTP` |
| `HTTP` | `http.py` | 构建 `APIRouter`；三个路由处理器：`create_upload`、`complete_upload`、`open_image` |
| `Service` | `service.py` | `create_upload(user_id, input_) -> (Image, str)`；`complete_upload(user_id, image_id) -> Image`；`open_for_user(user_id, image_id) -> str`；`open_for_session(user_id, session_id, image_id) -> str`；私有 `_discard(record)`；类方法 `build(store, config)` |
| `Store` | `store.py` | `insert(record)` / `delete(image_id)` / `mark_ready(image_id)` / `for_user(user_id, image_id, ready_only)` / `for_session(user_id, session_id, image_id)` |
| `ImageRecord` | `store.py` | 图片记录聚合对象（image、user_id、session_id、object_key、size_bytes、status） |
| `ImageNotFound` | `store.py` | 记录不存在异常（`Exception` 子类） |
| `create_schema(db)` | `store.py` | 建表 `chat_images`（`CREATE TABLE IF NOT EXISTS`） |
| `COSClient` | `cos.py` | `Protocol`：`sign_put(key)` / `sign_get(key)` / `head(key) -> ObjectInfo` / `delete(key)` |
| `COSConfig` | `cos.py` | 配置 dataclass（bucket/region/secret_id/secret_key）；`configured()` 判定配置是否真实可用 |
| `TencentCOS` | `cos.py` | 基于 `qcloud_cos`（`CosConfig`/`CosS3Client`）的腾讯云实现；`sign_put/sign_get` 用 `get_presigned_url`；`head/delete` 当前抛 `NotImplementedError` |
| `AgentInput` | `agent_input.py` | `async message_image_urls(user_id, session_id, image_ids) -> list[str]`：去重、非空校验后逐个调 `service.open_for_session` |
| `reference(image_id)` / `image_id_from_reference(value)` | `agent_input.py` | 图片引用标记编解码：`"edith-image://" + image_id`；解析返回 `(image_id, ok)` |
| `Image` / `UploadInput` | `types.py` | 领域类型（dataclass）：图片身份（id/mime_type）、上传输入（session_id/mime_type/size_bytes） |
| `CreateUploadRequest/Response`、`CompleteUploadRequest/Response` | `types.py` | Pydantic v2 API 模型，`alias_generator=to_camel`，请求体 `extra="forbid"` |

## 第三部分：与其他模块的联系

### ① 依赖关系

**内部模块**：

| 依赖 | 用途 |
| --- | --- |
| `internal.http_utils.EDITHError` | HTTP 层统一错误三元组（status_code / error_type / message），由组合根的 `register_error_handler` 转成 `{"type","message"}` JSON 响应 | `http.py:11` |
| `internal.agentrun`（被依赖方） | 本模块是 `agentrun` 的依赖：`agentrun.service.Dependencies.images` 注入 `images.agent_input`，`agentrun/configuration.py` 在构建模型输入时调用 `message_image_urls` | `main.py:92`、`agentrun/configuration.py:100-103` |

**外部 SDK / 库**：

| 依赖 | 用途 |
| --- | --- |
| `aiosqlite` | 异步 SQLite 连接；`db` 连接对象由组合根 `main.py` 的 `open_database` 创建后传入，模块不自行打开连接 | `store.py:8`、`module.py:8` |
| `qcloud_cos`（cos-python-sdk-v5） | 腾讯云 COS SDK；在 `TencentCOS.__init__` 内延迟导入，仅配置就绪时才加载 | `cos.py:47` |
| `fastapi` / `pydantic`（v2） | 路由（`APIRouter`、`RedirectResponse`）与请求/响应模型 | `http.py:8-9`、`types.py:10` |

**装配位置**：`main.py` 的 `lifespan` 中 `new_images_module(db, COSConfig(...))` 创建模块，`app.include_router(images.http.router)` 注册路由；数据库与 COS 配置均由组合根提供。

### ② 数据流向（一次典型业务流转）

```
前端                 HTTP 层(http.py)            服务层(service.py)              Store / COS
 │  POST /internal/images            │                       │                        │
 │  {userId, sessionId, mimeType, sizeBytes}                    │                        │
 ├─► 参数非空校验 ──────────────► create_upload               │                        │
 │                                  │   ① 校验 MIME∈{jpeg/png/webp}、1≤size≤10MB        │
 │                                  │   ② 生成 image.id="img_"+uuid4.hex                 │
 │                                  │   ③ object_key="chat-images/"+id                   │
 │                                  │   ④ insert(pending) ───────────────────────► 写 chat_images
 │                                  │   ⑤ sign_put(object_key) ──────────────────► COS 签发 PUT 签名
 │  ◄── {image, upload_url} ────────┤   （签名失败则 delete 回滚）                     │
 │  客户端用 upload_url 直传 COS 二进制 ──────────────────────────────────────────► COS
 │  POST /internal/images/{id}/complete │                       │                        │
 ├─► complete_upload ──────────────► ① for_user(ready_only=False)                    │
 │                                  │   ② head(object_key) ──────────────────────► COS 校验
 │                                  │   ③ 大小/MIME 匹配？ mark_ready ────────► status→ready
 │                                  │     不匹配则 _discard（COS delete + 删记录）
 │  GET /internal/images/{id}?userId= │                       │                        │
 ├─► open_image ──────────────────► open_for_user(ready_only=True)                   │
 │  ◄── 302 Redirect ───────────────┤   sign_get(object_key) ───────────────────► COS 签发 GET 签名
 │  浏览器访问短期 URL（Cache-Control: no-store）              │                        │
```

Agent 侧数据流：`agentrun` 收到带 `imageIds` 的消息后（`configuration.py`），调用 `images.message_image_urls(user_id, session_id, image_ids)`（按 user + session + ready 维度查库，逐张签发 GET 签名 URL），再在 `execution.py` 中把 URL 拼成 `{"type": "image_url", "image_url": {"url": url}}` 消息块注入模型调用。

### ③ 接口调用

**对外暴露的 HTTP 路由**（`http.py`，挂载于组合根 `main.py`）：

| Method | Path | 请求 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| `POST` | `/internal/images` | `CreateUploadRequest`（userId/sessionId/mimeType/sizeBytes，camelCase） | `CreateUploadResponse`（image + upload_url） | 预留上传：校验并落库，返回 COS PUT 签名 URL |
| `POST` | `/internal/images/{image_id}/complete` | `CompleteUploadRequest`（userId） | `CompleteUploadResponse`（image） | 确认上传完成：head 校验通过后置 `ready` |
| `GET` | `/internal/images/{image_id}` | query `userId` | `302 Redirect` 到短期 GET 签名 URL | 打开已确认图片（需 `ready`；`Cache-Control: no-store`） |

**内部方法及调用方**：

| 方法 | 调用方 |
| --- | --- |
| `Module.agent_input.message_image_urls(user_id, session_id, image_ids)` | `internal.agentrun` 的 `configuration.py`（通过 `main.py` 中 `Dependencies(images=images.agent_input)` 注入） |
| `reference()` / `image_id_from_reference()` | 目前**未发现**项目内调用方（Python 版尚未接线；对应 Go 版会话历史图片标记逻辑，待确认） |

## 第四部分：模块内容扩展说明

### ① 核心业务逻辑要点

1. **两阶段上传（预留-确认）**：`create_upload` 只做校验 + 落库（`pending`）+ 签发 PUT 签名 URL；客户端用签名 URL 直传 COS；`complete_upload` 通过 `COS head` 核对对象大小与 MIME 与预留一致后才 `mark_ready`，杜绝"预留 A 传 B"。
2. **校验规则**（`service.py`）：MIME 白名单 `_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp"}`；大小 `1 ≤ size_bytes ≤ MAX_IMAGE_BYTES`（`MAX_IMAGE_BYTES = 10 << 20`，即 10 MiB）；user_id/session_id 去空白后非空。
3. **对象键规范**：`object_key = "chat-images/" + image.id`，图片 ID 为 `"img_" + uuid4().hex`。
4. **签名有效期**：`SIGNED_URL_LIFETIME = 5 * 60`（5 分钟），PUT/GET 签名均适用。
5. **读取权限模型**：`open_for_user` 要求 `user_id` 匹配且 `status=ready`；`open_for_session`（供 agentrun）额外要求 `session_id` 匹配且 `ready`。
6. **失败回滚**：`create_upload` 中 `sign_put` 失败会 `store.delete(image.id)` 回滚记录后重抛；`complete_upload` 中 head 失败或校验不匹配会 `_discard`（尽力删除 COS 对象 + 删除记录）。
7. **无 COS 配置降级**：`COSConfig.configured()` 判定（任一字段空白或以 `your-` 占位开头即视为未配置）→ `Service._cos = None` → 上传/读取时抛 `RuntimeError("COS is not configured")`；`TencentCOS.head/delete` 当前直接抛 `NotImplementedError`（未实现，读取流程在 `complete_upload` 用到 head，因此无配置时完成确认不可用）。

### ② 配置项

本模块不直接读环境变量；由组合根 `main.py` 读取后构造 `COSConfig` 传入：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `EDITH_COS_BUCKET` | `""` | COS 存储桶名 |
| `EDITH_COS_REGION` | `""` | COS 地域 |
| `EDITH_COS_SECRET_ID` | `""` | 腾讯云 SecretId |
| `EDITH_COS_SECRET_KEY` | `""` | 腾讯云 SecretKey |

> 说明：`COSConfig.configured()` 会把以 `"your-"` 开头的占位值视为未配置；`EDITH_E2B_TEMPLATE` 等环境变量属其他模块（sandbox），本模块**未读取**。

### ③ 异常处理

| 异常 | 来源 | 处理策略 |
| --- | --- | --- |
| `ValueError`（参数/MIME/大小/重复） | `service.create_upload`、`complete_upload`、`agent_input.message_image_urls` | HTTP 层捕获并映射为 `EDITHError(400, "image_upload_rejected", ...)`；`agentrun` 侧捕获映射为 `Error("invalid_request", "load image input: ...")` |
| 参数缺失（HTTP 层自查） | `http.py` | `EDITHError(400, "invalid_request", ...)` |
| `ImageNotFound` | `store._read` | 冒泡；`open_image` 中任何异常统一映射为 `EDITHError(404, "image_not_found", "image not found")` |
| `RuntimeError`（COS 未配置） | `service.py` | 直接抛出，不映射为 400/404（配置类错误，待确认对外表现） |
| COS 签名/head 失败 | `cos.py` 调用点 | `create_upload` 回滚记录后重抛；`complete_upload` `_discard` 后重抛 |
| COS 清理失败 | `_discard` | 静默忽略（`except Exception: pass`），不影响记录删除 |
| 请求体校验失败 / 未知字段 | FastAPI `RequestValidationError` | 由组合根 `http_utils.register_error_handler` 统一为 `400 {"type":"invalid_request"}`（对齐 Go 版 `DisallowUnknownFields`） |

最终所有 `EDITHError` 由组合根的全局异常处理器输出固定 JSON 契约 `{"type": ..., "message": ...}`。

### ④ 扩展点

- **换存储 / 加 provider**：实现 `COSClient` Protocol 的 `sign_put / sign_get / head / delete`，在 `Service.build`（`service.py:32-37`）中替换 `TencentCOS` 即可；`cos.py` 顶部注释即声明了"接口化便于测试 mock 与无配置降级"。
- **放开 MIME / 大小限制**：改 `service.py` 的 `_SUPPORTED_MIME` 与 `MAX_IMAGE_BYTES`（`supported_mime_type` / `max_image_bytes` 为模块内查询入口）。
- **新增路由**：在 `HTTP._build_router`（`http.py:31`）内用 `@router.{method}(...)` 注册，复用 `self._service` 与现有 `EDITHError` 映射。
- **补全 COS 能力**：`TencentCOS.head` / `delete` 当前为 `NotImplementedError`，接入真实凭据后实现（对象删除可用于图片回收/过期清理）。
- **接线图片引用标记**：`agent_input.reference` / `image_id_from_reference` 已定义（`edith-image://` 前缀），当前无调用方，可在会话历史持久化/恢复逻辑中接入（对齐 Go 版 `session_images.go`，待确认）。
- **调整签名有效期**：改 `cos.py` 的 `SIGNED_URL_LIFETIME`。
