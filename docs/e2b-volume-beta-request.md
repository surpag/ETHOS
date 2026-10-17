# E2B Volumes private beta 访问申请（草稿）

> 目标：申请 E2B Volume（当前 private beta）访问权限。
> 发送地址：**support@e2b.dev**
> 发送邮箱：**请用你注册 E2B 账号的邮箱发**（E2B 要靠发件邮箱定位你的账号/团队）
> 需要你补齐的地方已用 `<...>` 标出。

---

**Subject:** Request for E2B Volumes (private beta) access

Hi E2B team,

I'd like to request access to **E2B Volumes**, which the docs note is currently in private beta.

**E2B account email:** `<注册 E2B 的邮箱>`

**What I'm building**
A multi-tenant cloud coding agent. Each user owns a *workspace* — a persistent set of
code files. Every conversation runs inside an E2B sandbox, where the agent reads/writes
files and runs `git` (clone / commit / push).

**Why I need Volumes**

1. **Workspace files must outlive a sandbox.** Today, anything the agent writes lives on
   the sandbox's ephemeral disk and is lost when the sandbox is killed or when the user
   opens a new session. The three natural ways to close that gap (object storage sync,
   git-only persistence, persistent volumes) all point at volumes as the cleanest option.
2. **I need to mount one volume into two sandboxes at the same time** — one where the
   agent works, and a separate, credential-isolated "runner" sandbox that performs
   `git push`. The agent's sandbox would never see the git token; the runner would only
   see the workspace. Shared-volume mounting is exactly the primitive this requires.

**Current behavior on my account**

```python
AsyncVolume.create(name="...")   # -> VolumeException: 403: use of volumes is not enabled
AsyncVolume.list()               # -> OK (0 volumes)
AsyncSandbox.create(...)         # -> OK, sandboxes work normally
```

**Environment / expected scale**

- E2B Python SDK `e2b==2.39.0`
- Template built from `e2bdev/base` (custom template with git already present)
- Expected scale: `<例如：约 10–50 个租户，峰值 <20 个并发沙箱，每个工作区 1 个 Volume>`

Happy to share more about the architecture or the use case if that helps.

Thanks,
`<你的名字>`
`<公司 / 项目 / 网站>`

---

## 备选：如果 E2B 询问更具体的用途

- **Volume 数量**：每个工作区 1 个；`<N>` 个工作区 → 约 `N` 个 Volume
- **挂载形态**：同一 Volume 同时挂到 2 个沙箱（agent 沙箱 + git runner 沙箱），
  两者都需要**读写**（agent 写代码，runner 写 `.git` 对象）
- **数据规模**：单工作区典型 `<10–200 MB>`，大量小文件（源码树，可能上万文件）
- **生命周期**：Volume 与工作区同寿命（用户主动删除工作区才销毁）
