# 沙箱工作区同步优化：按需同步 + 降频（dirty 标记）

> 状态：**已实施**（2026-09-07）
> 背景：每次工具调用都把用户的所有 workspace 全量同步进沙箱，大目录（3 万+ 文件）`write_files` 超时 60 秒，导致单次工具调用耗时 164 秒、Agent 拿不到文件。

---

## 一、问题根因

```
每次工具调用 → Service.workspace() → _sync_workspace_files()
  → sync_to_sandbox() → sync_all_to_sandbox()   # 遍历 os.listdir(root) 同步「所有」workspace
  → sync_workspace_to_sandbox()                 # rglob 全目录 + write_files 全量写
```

三层叠加：
1. **每次工具调用都 sync**：沙箱已存在时仍重复同步。
2. **sync_all 同步所有 workspace**：一个会话只属于一个 workspace，却同步了全部。
3. **全量非增量**：`rglob` + `write_files` 全量写，3 万文件必超时。

实测：两个 workspace 各超时 60 秒，单次 `sandbox_list_files` 耗时 164 秒。

---

## 二、方案 A：按需同步（只同步当前 workspace）

把同步语义从「全部 workspace」改为「指定 workspace」，由 `workspace_id` 显式传入。

### 改动点

| 文件 | 改动 |
|---|---|
| `workspace/volume_backend.py` | `VolumeBackend.sync_all_to_sandbox(sandbox)` → `sync_workspace_to_sandbox(workspace_id, sandbox)`（抽象方法，E2B no-op 默认）；**删除** `LocalVolumeBackend.sync_all_to_sandbox`（遍历所有） |
| `workspace/volume_service.py` | `sync_to_sandbox(sandbox, workspace_id)` 传 workspace_id，调 `backend.sync_workspace_to_sandbox(workspace_id, sandbox)` |
| `sandbox/service.py` | `workspace(user_id, session_id, workspace_id="")` 加 workspace_id 参数，透传到 sync |
| `sandbox/tools.py` | `execute` 里 `self._service.workspace(user_id, session_id, workspace_id)` 传入 workspace_id |

---

## 三、方案 B：降频同步（dirty 标记）

同步不再「每次工具调用都做」，只在两个时机触发：
1. **沙箱新建**（`force=True`，初始同步）
2. **文件上传后**（`write_files` 置 dirty，下次工具调用检测到 dirty 才同步）

### 改动点

| 文件 | 改动 |
|---|---|
| `workspace/volume_service.py` | `__init__` 加 `self._dirty: set[str]`；`write_files` 写成功后 `self._dirty.add(workspace_id)`；`sync_to_sandbox` 成功后 `discard`；新增 `is_dirty(workspace_id)` |
| `sandbox/service.py` | `_sync_workspace_files(workspace, workspace_id="", force=False)`：`force or is_dirty` 才同步；`workspace()` connect 分支走 dirty 判断、create 分支 `force=True`；`existing_workspace()` **去掉 sync**（前端文件面板已走本地直读 P0） |

---

## 四、效果

| 指标 | 改前 | 改后 |
|---|---|---|
| 每次工具调用的 sync 次数 | 每次全量同步所有 workspace | 仅 dirty 时同步当前 workspace |
| 单次工具调用耗时 | 164 秒（2 个 workspace × 60s 超时） | 秒级（无 dirty 时零 sync） |
| 同步范围 | 所有 workspace | 仅当前会话所属 workspace |

---

## 五、验证

- ruff：4 文件 All checks passed
- import：无循环依赖
- sanity check：dirty 标记（写后 dirty / sync 后清除）+ 按需同步（只 sync 指定 workspace_id）通过
- pytest：71 passed、2 预存在失败（`test_models_catalog`、`test_build_messages_order`，与本次无关）

---

## 六、后续可优化（未做）

1. **增量同步**：`mtime` 对比只同步变更文件，替代全量 `rglob + write_files`（大目录场景的根本优化）。
2. **dirty 持久化**：当前 dirty 存进程内存，重启后丢失（但重启后沙箱也会重建，走 create 分支 force sync，可自愈）。
3. **sync 超时拆分**：单次 `write_files` 全量写超时，可考虑分批写入（对齐前端上传的分批策略）。
