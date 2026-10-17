# 沙箱运行时后端抽象（Sandbox Runtime Abstraction）

> 状态：规划稿（**未实施**，作为未来优化参考）
> 目标：把「沙箱命令执行 / 文件操作」从 E2B SDK 的具体类型中解耦，为后续 Docker 本地沙箱预留可替换边界。
> 定位：本文档是「未来要换后端时再读」的路线图，不描述已完成的改动。

---

## 一、背景与动机

2026-09-04 在真实 E2B 环境复现出一个硬故障：`sandbox_run_command` 无论执行什么命令，都返回同一错误

```
Commands.run() got an unexpected keyword argument 'args'
```

并导致 Agent 陷入无效重试，整个 run 空转约 24 分钟（`duration_ms=1457584.69`、`tokens=0`、`tools_called=8`、`reason=run.completed`）。

两层原因：

1. **直接原因（SDK 版本不匹配）**：安装的 `e2b==2.39.0`，其 `Commands.run()` 签名已无 `args` 形参，而代码仍按旧版 API 传 `args=`。
2. **深层原因（无抽象边界）**：`AsyncSandbox`（E2B 具体类型）一路穿透到工具层与 Service 层。SDK 签名一改，8 个工具全部失效——这是「换后端就要重写工具层」的根源。

后续计划：沙箱可能从云沙箱（E2B）切换为 **Docker 本地实现**。届时命令执行、文件读写、进程管理三套 API 全变，参数形态也不同。因此需要提前沉淀一套契约。

---

## 二、现状盘点（代码实锤）

### 2.1 工具层对 E2B 的耦合面

代码位置：`backend-py/src/edith_backend/internal/sandbox/tools.py`

8 个 `_op_*` 方法签名全部为 `(sandbox: AsyncSandbox, args: dict)`，直接调用 E2B SDK：

| 工具 | E2B API 调用 |
|---|---|
| `sandbox_list_files` | `sandbox.files.list` |
| `sandbox_read_file` | `sandbox.files.read` |
| `sandbox_write_file` | `sandbox.files.write` |
| `sandbox_make_directory` | `sandbox.files.make_dir` |
| `sandbox_move_path` | `sandbox.files.rename` |
| `sandbox_delete_path` | `sandbox.files.remove` |
| `sandbox_run_command` | `sandbox.commands.run`（**含 args= bug**） |
| `sandbox_start_process` | `sandbox.commands.run(background=True)`（同款 args= bug） |
| `sandbox_list_processes` | `sandbox.commands.list` |
| `sandbox_kill_process` | `sandbox.commands.kill` |

除签名外，`_op_*` 还依赖 E2B SDK 的返回对象：`result.stdout / stderr / exit_code`、`p.pid / p.cmd / p.args / p.cwd / p.tag`。这些对象形态在换后端后全变。

### 2.2 Service 层的耦合

代码位置：`backend-py/src/edith_backend/internal/sandbox/service.py`

- `workspace()` / `existing_workspace()` 返回类型就是 `AsyncSandbox`；
- `AsyncSandbox.connect / create`、`SandboxLifecycle`、`volume_mounts` 均直接使用 E2B SDK。

### 2.3 已有的抽象先例（可对齐）

代码位置：`backend-py/src/edith_backend/internal/workspace/volume_backend.py`

- `VolumeBackend`（ABC）+ `E2BVolumeBackend` / `LocalVolumeBackend` 双实现；
- `new_backend(kind)` 工厂按 `EDITH_WORKSPACE_VOLUME_BACKEND=e2b|local` 选择。
- **残留泄漏**：`sync_all_to_sandbox(sandbox: AsyncSandbox)` / `sync_workspace_to_sandbox(..., sandbox: AsyncSandbox)` 仍依赖 `sandbox.files.write_files`，即「文件存哪」已解耦、但「往沙箱里同步」这一动作仍绑死 E2B。

结论：本项目已认同「后端可替换」方向，缺的是把同一思路**补齐到命令执行与文件操作**。

---

## 三、目标设计

### 3.1 核心原则

- **只抽象「工具层实际用到的原语」这一薄层，不抽象「整个沙箱」。**
- **出口一律用自有值对象，禁止 `AsyncSandbox` 外泄。**

### 3.2 契约形状（示意）

```python
@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    stdout_truncated: bool
    stderr_truncated: bool

@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int

@dataclass
class ProcessInfo:
    pid: int
    command: str
    cwd: str


class SandboxRuntime(ABC):
    # 生命周期
    async def connect(self, sandbox_id: str) -> None: ...
    async def create(self, *, user_id: str, session_id: str) -> str: ...  # 返回 sandbox_id
    async def destroy(self, sandbox_id: str) -> None: ...

    # 文件操作
    async def list_files(self, path: str, depth: int) -> list[FileEntry]: ...
    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str) -> None: ...
    async def make_dir(self, path: str) -> None: ...
    async def rename(self, src: str, dst: str) -> None: ...
    async def remove(self, path: str) -> None: ...

    # 进程操作
    async def run_command(self, cmd: str, *, envs: dict | None, cwd: str,
                          timeout: float) -> CommandResult: ...
    async def start_process(self, cmd: str, *, cwd: str) -> int: ...      # 返回 pid
    async def list_processes(self) -> list[ProcessInfo]: ...
    async def kill_process(self, pid: int) -> None: ...
```

要点：

1. 方法入口用「字符串 + 值对象」，**不出现 `AsyncSandbox` / `CommandResult`（SDK 版）**。
2. `CommandResult` 等是**项目自有** dataclass，SDK 对象在实现内部就地转换。
3. 工具层（`SandboxToolSet`）只依赖 `SandboxRuntime`，不再 import `e2b`。

### 3.3 工厂

```python
def new_runtime(kind: str) -> SandboxRuntime:
    if kind == "docker":
        return DockerSandboxRuntime()
    return E2BSandboxRuntime()   # 默认
```

由 `EDITH_SANDBOX_BACKEND=e2b|docker` 选择，与 `new_backend(kind)` 对齐。

---

## 四、反过度设计（Rule of Three）

- **现在只做**：最小接口 + `E2BSandboxRuntime`（顺带修掉 `args=` bug）+ 工厂。
- `stdin`、流式 `on_stdout` 回调、超时重试、cwd 路径转换等细节，**先在各实现内部自处理**，不要预塞进接口。
- Docker 是**第二个实现**，等它落地时再按真实需求收敛接口；第三个后端出现时才谈通用化。
- 不为一处抽象引入抽象基类 + 反射 + 插件机制等「灵活性」，保持接口越小越好。

---

## 五、Docker 本地隔离模型风险（真正要提前设计的）

E2B 是远程隔离沙箱，`rm -rf /` 最多毁一个沙箱；Docker 本地 `exec` 之后，隔离边界完全取决于部署方如何配置。换 Docker 前必须先回答：

| 风险 | 要确认的点 |
|---|---|
| 运行用户 | 命令以非 root 运行，禁止容器内提权 |
| 卷挂载防逃逸 | workspace 只挂载指定目录；能否通过 `../` 或符号链接逃逸到宿主机路径 |
| 资源配额 | CPU / 内存 / 磁盘上限；单个死循环命令不能拖垮宿主机 |
| 网络隔离 | 容器能否访问内网 / 能否反打宿主（SSRF 面） |
| 生命周期 | 容器/进程的创建、超时销毁、孤儿进程回收 |

> 参数不匹配是分钟级修复的小事；隔离模型错了是宿主机失守。两者优先级不在一个量级。

---

## 六、实施顺序建议

1. **P0 止血**：修 `tools.py` 两处 `args=`（去掉该关键字，命令直接拼进 `cmd` 字符串）——不影响接口，先恢复功能。
2. **P1 抽象**：抽 `SandboxRuntime` + `E2BSandboxRuntime`，把 8 个 `_op_*` 逻辑搬进实现，工具层改为只依赖接口；工厂默认 E2B。
3. **P2 换后端**：实现 `DockerSandboxRuntime`（`docker exec` + 本地文件 IO），先跑通再谈隔离加固。
4. **P2 隔离加固**：按第五节清单逐项落实并补测试。

---

## 附：关键代码位置索引

| 功能 | 文件 | 位置 |
|---|---|---|
| 工具层（8 个 `_op_*`，绑死 AsyncSandbox） | `internal/sandbox/tools.py` | `_op_sandbox_*`，尤其 `_op_sandbox_run_command`（args= bug） |
| Service 层（返回 AsyncSandbox） | `internal/sandbox/service.py` | `workspace` / `existing_workspace` |
| Volume 后端抽象（先例） | `internal/workspace/volume_backend.py` | `VolumeBackend` / `new_backend` |
| e2b SDK 签名（2.39.0，无 args） | `.venv/Lib/site-packages/e2b/sandbox_async/commands/command.py` | `Commands.run`（184 行起） |
| 工作区布局常量 | `internal/workspace/layout.py` | `WORKSPACE` |
