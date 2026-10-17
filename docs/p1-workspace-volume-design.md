# P1 设计稿：工作区文件真实持久化（Volume 落地）

> 状态：设计稿（未实施，用户自己实现）
> 背景：P0 已完成「工作区 = 逻辑容器」（只记 metadata 文件清单，不真正上传）。P1 把选中的文件夹**真实上传到 E2B Volume**，沙箱挂载后可读写，Agent 能访问项目代码。
> 关联：`docs/p0-phase-a-design.md`（Phase A，工作区驱动）、`docs/p0-project-memory-design.md`（v1）
> 改动原则：在 P0 基础上增量改造，不破坏既有「选文件夹→建工作区→写指令→注入」链路。

---

## 一、设计目标

| 维度 | P0（现状） | P1（本次） |
|------|-----------|-----------|
| 工作区文件 | 只存 metadata（fileCount/filePaths 清单） | **真实上传到 E2B Volume** |
| Agent 访问 | 看不到项目文件 | 沙箱文件工具可 list/read/write 项目文件 |
| 删除 | 删记录 + 级联删指令 | + **销毁 Volume** |
| 会话恢复 | Sandbox 无工作区 | 沙箱创建时挂载工作区 Volume |

---

## 二、数据模型（1 张表加 2 列）

### `workspaces` 表加列

```sql
ALTER TABLE workspaces ADD COLUMN volume_id TEXT;     -- E2B Volume id（懒创建，首次上传才建）
ALTER TABLE workspaces ADD COLUMN volume_token TEXT;  -- 该 Volume 的 token（连接用）
```

- 幂等迁移：`PRAGMA table_info(workspaces)` 检查，缺列则 `ALTER TABLE ADD COLUMN`（对齐现有 `_migrate` 模式）
- 语义：`volume_id IS NULL` = 尚未创建 Volume（用户建了工作区但没传文件，或传文件失败回滚）
- **metadata 不变**：`{folderName, fileCount, filePaths}` 继续记录前端展示信息

---

## 三、接口设计

### 3.1 新增：上传工作区文件

```
POST /internal/workspaces/{workspace_id}/files
Content-Type: multipart/form-data
表单字段: file（可多个 file 字段）
```

**后端流程**：
1. 校验 `workspace_id` 归属（user_id 从 query 传）
2. 惰性创建 Volume（首次上传）：`WorkspaceStore.ensure_volume(user_id, workspace_id)` →
   `AsyncVolume.create(name=f"edith-ws-{workspace_id[:12]}")` → 存 `volume_id/volume_token`
3. 逐文件写 Volume：
   ```python
   volume = await AsyncVolume.connect(volume_id, api_key=volume_token)  # 或直接 AsyncVolume.connect(volume_id)
   await volume.make_dir(dir_path, force=True)
   await volume.write_file(rel_path, file_bytes, force=True)
   ```
4. 更新 `metadata.fileCount` / `metadata.filePaths`（追加新路径）
5. 返回 `{uploaded: n, fileCount: 总数}`

**安全约束**：
- 单文件 ≤ 50MB（对齐现有上传）；单次请求总大小 ≤ 200MB（可配）
- 文件名清洗：拒绝 `..`、`/`、`\` 开头的绝对路径；只保留 `相对路径`（前端传 `webkitRelativePath`）
- **懒创建 + 幂等**：`ensure_volume` 已存在则直接返回，不重复建

**响应示例**：
```json
{ "uploaded": 12, "fileCount": 12 }
```

### 3.2 现有接口不变（P0 已有）

| 接口 | 说明 |
|------|------|
| POST/GET `/internal/workspaces` | 创建/列表（创建时不建 Volume，保持 P0 语义） |
| DELETE `/internal/workspaces/{id}` | **改造**：若有 volume → `AsyncVolume.destroy` → 删记录 + 级联删指令 |
| GET/PUT/DELETE `/internal/workspaces/{id}/instructions` | 指令（不变） |
| GET/PUT/DELETE `/internal/workspaces/{id}/memories` | 自动记忆（不变） |

---

## 四、后端实现要点（按文件）

### 4.1 `userconfig/schema.py`
- `workspaces` 建表语句加 `volume_id TEXT`、`volume_token TEXT`
- `_migrate` 加第 4 步：检查 volume 列并 ALTER（幂等）

### 4.2 `userconfig/workspace_store.py`
- `WorkspaceStore` 加方法：
  - `save_volume(user_id, workspace_id, volume_id, volume_token)` → UPDATE
  - `load_volume(user_id, workspace_id) -> tuple[str, str] | None` → 返回 `(volume_id, volume_token)`
- `delete()` 改造：删记录前查 volume，有则调用**回调销毁**（解耦 E2B 依赖，见 4.4）
  ```python
  async def delete(self, user_id, workspace_id, on_destroy_volume=None) -> bool:
      record = await self._load_volume(...)
      if record: await on_destroy_volume(record[0])  # 回调：AsyncVolume.destroy
      # 原有级联删除逻辑
  ```

### 4.3 新建 `userconfig/workspace_volume.py`（Volume 操作封装）
```python
class WorkspaceVolumeService:
    """工作区 Volume 的懒创建、文件写入、销毁。依赖注入 E2B，便于测试。"""

    def __init__(self, store: WorkspaceStore) -> None: ...

    async def ensure_volume(self, user_id, workspace_id) -> AsyncVolume:
        """已存在则连接返回；否则创建并持久化。"""

    async def write_files(self, user_id, workspace_id, files: list[(rel_path, bytes)]) -> int:
        """写入一批文件到 Volume；更新 metadata.fileCount/filePaths。"""

    async def destroy(self, user_id, workspace_id) -> None:
        """销毁 Volume 并清空 volume_id/volume_token。"""
```

**关键决策：metadata 更新放这里**——写 Volume 成功后同步更新 `metadata.filePaths`（前端无需二次调用）。

### 4.4 `userconfig/workspace_http.py`
- `build_workspace_router` 加 `POST /internal/workspaces/{id}/files`
  - 需要访问 `WorkspaceVolumeService`（构造时注入）
  - DELETE 改传销毁回调：`store.delete(..., on_destroy_volume=volume_service.destroy_by_id)`

### 4.5 `main.py`
- 组装：`workspace_volumes = WorkspaceVolumeService(workspaces)`
- `build_workspace_router(workspaces, workspace_volumes)`

### 4.6 `sandbox/service.py`（挂载工作区 Volume）
**核心改动**：沙箱创建时，除了挂用户 Skills Volume，还挂该用户**所有工作区 Volume**：

```python
mounts: dict[str, AsyncVolume | str] = {}
# 1. 用户 Skills Volume（现有逻辑）
user_mount = await self._volumes.mount_for_user(user_id)   # 现有
# 2. 用户全部工作区 Volume（新增）
ws_volumes = await workspace_volumes.list_mounts(user_id)   # [(volume_id, workspace_id), ...]
for volume_id, workspace_id in ws_volumes:
    mounts[volume_id] = f"/home/user/workspaces/{workspace_id}"
```

- `volume_mounts` 签名是 `Dict[str, Union[AsyncVolume, str]]`（**同一 Volume 可挂多个路径**，直接传 `volume_id` 字符串即可）
- **为什么挂全部而非当前一个**：Sandbox 是 session 粒度、创建后不重建；用户切换工作区后同一 session 仍要能访问旧工作区文件。数量有限（用户工作区个数），可接受
- **路径约定**：`/home/user/workspaces/{workspace_id}/`（对齐 `WorkspaceLayout.root = /home/user`，现有文件工具 `workspace_path()` 无需改动即可访问该目录，因为相对路径是 `workspaces/{id}/xxx`）

### 4.7 `sandbox/workspace.py`（工具提示更新，可选）
- `tool_guide()` 追加一句：`{workspaces}/ 放项目文件（对应你选择的工作区），{uploads}/ 放本次上传文件`

---

## 五、前端实现要点（按文件）

### 5.1 `lib/sandbox/files.ts`（新增）
```ts
export async function uploadWorkspaceFiles(
  workspaceId: string,
  files: { path: string; blob: Blob }[],
): Promise<{ uploaded: number; fileCount: number }>
```
- `FormData` 逐文件 append：`formData.append("file", blob, relPath)`
- `fetch(/api/workspaces/${workspaceId}/files)` → 解析

### 5.2 `app/api/workspaces/[id]/files/route.ts`（新建 BFF）
- Clerk 校验 → 转发 multipart 到后端（**注意**：Next.js Route Handler 转发 FormData 需 `headers: { "X-Request-ID": ... }`，body 原样传）
- 校验：单文件 ≤50MB 前置拦截

### 5.3 `components/chat/workspace-switcher.tsx`（改造创建流程）
```
当前（P0）: 选文件夹 → 确认弹窗 → POST /api/workspaces（只带 metadata）
改为（P1）:
  选文件夹 → 确认弹窗（可改名）→ POST /api/workspaces（创建，无文件）
    → 成功后 uploadWorkspaceFiles(workspaceId, 文件列表)   // 逐文件上传
    → 进度提示「正在上传 12 个文件…」→ 完成后自动选中新工作区
    → 失败：提示 + 保留空工作区（或提供「重试上传」按钮）
```

### 5.4 文件读取路径来源（前端不用改）
- Agent 侧通过沙箱文件工具访问 `/home/user/workspaces/{id}/`，前端无需展示文件树（除非后续做工作区面板）

---

## 六、删除级联（重点确认）

```
DELETE /internal/workspaces/{id}
  → 查 volume_id
  → 有: AsyncVolume.destroy(volume_id)  （E2B 失败仅 warning，不阻断 SQL 删除）
  → 删 workspaces 记录
  → 级联删 project_memories（指令 + 自动记忆）
  → 前端二次确认弹窗文案更新：「将同时删除项目指令、自动记忆和云端项目文件」
```

---

## 七、验收标准

1. 新建工作区（选文件夹）→ 文件真实出现在 E2B Volume（`AsyncVolume.list` 验证）
2. 沙箱工具可访问：`sandbox_list_files(path="workspaces/{id}")` 返回上传的文件
3. Agent 读取项目文件成功（对话中让 Agent 读 `workspaces/{id}/README.md` 能返回内容）
4. 删除工作区 → Volume 被销毁（`AsyncVolume.exists` 为 False）+ 指令/记忆级联删除
5. 重复上传同文件 → 覆盖（force=True），metadata.filePaths 不重复
6. 懒创建：只建工作区不传文件 → `volume_id IS NULL`，不产生 E2B 资源
7. E2B 不可用时降级：上传失败返回明确错误，不影响既有聊天功能
8. pytest + ruff + tsc 全绿

---

## 八、改动清单

| 文件 | 改动 |
|------|------|
| `userconfig/schema.py` | workspaces 加 volume_id/volume_token 列 + 幂等迁移 |
| `userconfig/workspace_store.py` | +save_volume/load_volume；delete 支持销毁回调 |
| `userconfig/workspace_volume.py`（新建） | WorkspaceVolumeService：ensure_volume/write_files/destroy |
| `userconfig/workspace_http.py` | +POST files 接口；DELETE 传销毁回调 |
| `userconfig/types.py` | +WorkspaceFilesUploadResponse |
| `main.py` | 组装 WorkspaceVolumeService |
| `sandbox/service.py` | 沙箱创建时挂载全部工作区 Volume |
| `sandbox/workspace.py` | tool_guide 补 workspaces/ 说明（可选） |
| `web-v1/lib/sandbox/files.ts` | +uploadWorkspaceFiles |
| `web-v1/app/api/workspaces/[id]/files/route.ts`（新建） | BFF 转发 multipart |
| `web-v1/components/chat/workspace-switcher.tsx` | 创建后上传文件 + 进度 + 失败重试 |
| 测试 | test_stage9 补 volume 相关（mock E2B） |

---

## 九、风险与降级

| 风险 | 对策 |
|------|------|
| E2B Volume 数量上限（免费层通常 5-10 个） | 懒创建（不传文件不建）；删除即销毁；文档提示上限 |
| 单文件过大 | 50MB 限制前置校验 |
| 上传中断 | 文件级幂等（force 覆盖）；失败保留空工作区可重试 |
| Volume 被销毁但 Sandbox 已挂载 | E2B 挂载的是 id，销毁后访问报错 → 工具返回明确错误，不影响其他路径 |

---

## 十、面试话术

> P0 做了「工作区 = 逻辑容器」，但只有文件清单没有真实文件。P1 把选中的文件夹真实上传到 E2B Volume：懒创建（首次上传才建，避免空工作区烧 Volume 配额）、沙箱创建时挂载该用户全部工作区 Volume（路径 `/home/user/workspaces/{id}/`，复用现有文件工具的路径校验，零改工具）、删除工作区时销毁 Volume + 级联删指令和自动记忆。设计上把 E2B 依赖收敛到 WorkspaceVolumeService 一个类，Store 只做数据、回调销毁，便于单测 mock。
