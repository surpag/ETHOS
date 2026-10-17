"""gitops 模块测试：命令白名单、输出脱敏、非交互环境、SSRF、执行器（纯逻辑 + fake 沙箱）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from edith_backend.internal.gitops import (
    CloneUrlRejected,
    GitCommandRejected,
    assert_safe_clone_url,
    non_interactive_env,
    redact_git_output,
    run_git,
    validate_git_command,
)

# ---- policy ----

def test_read_only_subcommands_allowed() -> None:
    for sub in ("status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "grep"):
        validate_git_command(sub, ["--stat"])


def test_destructive_subcommands_rejected() -> None:
    """M3 后仍被拒：破坏性 / 交互式 / 历史改写类子命令。"""
    for sub in ("reset", "rebase", "clean", "merge", "branch", "checkout", "stash", "cherry-pick"):
        with pytest.raises(GitCommandRejected):
            validate_git_command(sub, [])


def test_local_write_allowed_remote_gated() -> None:
    """M3：add/commit 默认放行；push/pull/fetch 仅 allow_remote=True 时放行（runner 专用）。"""
    validate_git_command("add", ["src/a.py"])
    validate_git_command("commit", ["-m", "msg"])
    for sub in ("push", "pull", "fetch"):
        with pytest.raises(GitCommandRejected):
            validate_git_command(sub, [])
        validate_git_command(sub, ["origin", "main"], allow_remote=True)


def test_add_requires_explicit_paths() -> None:
    """git add 只收显式路径：拒绝 . / -A / 通配符（抄 wuu）。"""
    for bad in ([], ["."], ["-A"], ["--all"], ["-u"], ["src/*"], ["--patch"]):
        with pytest.raises(GitCommandRejected):
            validate_git_command("add", bad)
    validate_git_command("add", ["src/a.py", "docs/b.md"])


def test_commit_requires_message_and_blocks_audit_flags() -> None:
    """commit 必须带消息；--no-verify/--author/--date/-c 等审计敏感 flag 一律拒。"""
    for bad in (
        [], ["--amend"], ["-m", "x", "--no-verify"], ["--author", "a", "-m", "x"],
        ["-m", "x", "--date", "now"], ["-c", "x", "-m", "y"],
    ):
        with pytest.raises(GitCommandRejected):
            validate_git_command("commit", bad)
    validate_git_command("commit", ["-m", "feat: x"])
    validate_git_command("commit", ["--message=feat: x"])


def test_remote_blocks_dangerous_flags() -> None:
    """远端命令禁 force / 镜像 / 删分支 / 跳过 hook。"""
    for bad in (
        ["--force"], ["-f", "origin", "main"], ["--mirror"], ["--no-verify"],
        ["--force-with-lease"], ["--delete", "b"],
    ):
        with pytest.raises(GitCommandRejected):
            validate_git_command("push", bad, allow_remote=True)
    validate_git_command("push", ["origin", "main"], allow_remote=True)
    validate_git_command("pull", ["--rebase"], allow_remote=True)


def test_global_arg_prefixes_rejected() -> None:
    with pytest.raises(GitCommandRejected):
        validate_git_command("status", ["--config-env=x=y"])
    with pytest.raises(GitCommandRejected):
        validate_git_command("log", ["--exec-path=/tmp/x"])


def test_shell_metachars_rejected() -> None:
    for bad in ("a;rm -rf /", "x|y", "$(whoami)", "`id`", "a>b"):
        with pytest.raises(GitCommandRejected):
            validate_git_command("log", [bad])


# ---- redact ----

def test_redact_bearer_and_tokens() -> None:
    assert redact_git_output("Authorization: Bearer abc123def") == "<redacted>"
    assert "abc123" not in redact_git_output("curl -H 'Authorization: Bearer abc123def' x")
    assert "secret99" not in redact_git_output("api_key=secret99")
    assert redact_git_output("access_token: superToken123") == "<redacted>"


def test_redact_url_embedded_credential() -> None:
    out = redact_git_output("origin https://x-access-token:ghp_ABCDEFGH@github.com/o/r.git (fetch)")
    assert "ghp_ABCDEFGH" not in out
    assert "<redacted>" in out


def test_redact_passthrough() -> None:
    assert redact_git_output("On branch main\nnothing to commit") == "On branch main\nnothing to commit"
    assert redact_git_output("") == ""


# ---- env ----

def test_non_interactive_env() -> None:
    env = non_interactive_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_PAGER"] == "cat"
    assert env["EDITOR"] == "true"
    # 返回新 dict，调用方改它不影响常量
    env["EDITOR"] = "vim"
    assert non_interactive_env()["EDITOR"] == "true"


def test_git_identity_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """提交身份：用户设置优先 → 环境变量 → 平台默认。"""
    from edith_backend.internal.gitops import resolve_git_identity

    monkeypatch.delenv("EDITH_GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("EDITH_GIT_AUTHOR_EMAIL", raising=False)
    assert resolve_git_identity() == ("EDITH Agent", "agent@edith.local")
    assert resolve_git_identity("Alice", "a@x.com") == ("Alice", "a@x.com")
    monkeypatch.setenv("EDITH_GIT_AUTHOR_NAME", "Platform")
    monkeypatch.setenv("EDITH_GIT_AUTHOR_EMAIL", "p@x.com")
    assert resolve_git_identity() == ("Platform", "p@x.com")
    # 用户设置优先；部分留空时逐字段回退
    assert resolve_git_identity("Bob", "") == ("Bob", "p@x.com")


def test_git_identity_env_shape() -> None:
    """身份 env 同时设置 author 与 committer（用 env 而非 `-c`，后者被禁）。"""
    from edith_backend.internal.gitops import git_identity_env

    assert git_identity_env("Alice", "a@x.com") == {
        "GIT_AUTHOR_NAME": "Alice",
        "GIT_AUTHOR_EMAIL": "a@x.com",
        "GIT_COMMITTER_NAME": "Alice",
        "GIT_COMMITTER_EMAIL": "a@x.com",
    }


# ---- ssrf ----

def test_ssrf_blocks_private_and_metadata() -> None:
    for url in (
        "http://localhost/repo.git",
        "https://127.0.0.1/r.git",
        "https://10.0.0.5/r.git",
        "https://192.168.1.1/r.git",
        "https://169.254.169.254/latest/meta-data",
        "https://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/passwd",
        "git://github.com/x/y.git",
    ):
        with pytest.raises(CloneUrlRejected):
            assert_safe_clone_url(url)


def test_ssrf_allows_public_https_ssh_scp() -> None:
    assert_safe_clone_url("https://github.com/o/r.git")
    assert_safe_clone_url("ssh://git@github.com/o/r.git")
    assert_safe_clone_url("git@github.com:o/r.git")


# ---- executor（fake 沙箱）----

class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.output = "ok"

    async def run(self, cmd: str, **kwargs) -> SimpleNamespace:
        self.calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(exit_code=0, stdout=self.output, stderr="")


def test_executor_applies_env_and_redacts() -> None:
    async def go():
        cmds = FakeCommands()
        cmds.output = "origin https://x-access-token:ghp_SECRET@github.com/o/r.git (fetch)"
        sandbox = SimpleNamespace(commands=cmds)
        result = await run_git(sandbox, "/home/user/workspace/w1", "ls-remote", [])
        assert "ghp_SECRET" not in result["stdout"]
        assert "<redacted>" in result["stdout"]
        assert cmds.calls[0]["cwd"] == "/home/user/workspace/w1"
        assert cmds.calls[0]["envs"]["GIT_TERMINAL_PROMPT"] == "0"

    asyncio.run(go())


def test_executor_quotes_args_and_blocks_bad_clone() -> None:
    async def go():
        cmds = FakeCommands()
        sandbox = SimpleNamespace(commands=cmds)
        await run_git(sandbox, "/w", "log", ["--pretty=oneline"])
        assert cmds.calls[0]["cmd"].startswith("git log ")
        # shell 元字符被白名单拒绝，不会到执行层（含 --pretty 里的 | 也一并拒绝，对齐 wuu）
        with pytest.raises(GitCommandRejected):
            await run_git(sandbox, "/w", "log", ["a;b"])
        with pytest.raises(GitCommandRejected):
            await run_git(sandbox, "/w", "log", ["--pretty=format:%h|%s"])
        with pytest.raises(CloneUrlRejected):
            await run_git(sandbox, "/w", "clone", ["https://169.254.169.254/x.git"])

    asyncio.run(go())


def test_executor_treats_nonzero_exit_as_result() -> None:
    """git 非零退出（非仓库/冲突）应是正常结果而非异常，且 stderr 同样脱敏。"""
    from e2b.sandbox.commands.command_handle import CommandExitException

    class Raiser:
        async def run(self, cmd: str, **kwargs) -> None:
            raise CommandExitException(
                stderr="fatal: unable to access 'https://x-access-token:ghp_LEAKED@github.com/o/r.git/'\n",
                stdout="",
                exit_code=128,
                error="authentication failed",
            )

    async def go():
        result = await run_git(SimpleNamespace(commands=Raiser()), "/w", "status", [])
        assert result["exitCode"] == 128
        assert "ghp_LEAKED" not in result["stderr"]
        assert "<redacted>" in result["stderr"]

    asyncio.run(go())


# ---- D-2：bundle 导出 + 后端 push ----

def test_export_bundle_reads_and_cleans() -> None:
    """export_bundle：跑 bundle create → 读回字节 → 清理临时文件。"""
    from edith_backend.internal.gitops.bundle import export_bundle

    class Cmds:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, cmd: str, **kwargs) -> SimpleNamespace:
            self.calls.append(cmd)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class Files:
        async def read(self, path: str, format: str = "text") -> bytes:
            return b"BUNDLE-BYTES"

    async def go() -> None:
        cmds = Cmds()
        sandbox = SimpleNamespace(commands=cmds, files=Files())
        data = await export_bundle(sandbox, "/w", "main")
        assert data == b"BUNDLE-BYTES"
        assert any("bundle create" in c and "main" in c for c in cmds.calls)
        assert any(c.startswith("rm -f ") for c in cmds.calls)

    asyncio.run(go())


def test_export_bundle_all_refs_and_failure() -> None:
    """branch 为空 → 用 --all；非零退出 → BundleExportError。"""
    from edith_backend.internal.gitops.bundle import BundleExportError, export_bundle

    class Cmds:
        def __init__(self, code: int) -> None:
            self.code, self.calls = code, []

        async def run(self, cmd: str, **kwargs) -> SimpleNamespace:
            self.calls.append(cmd)
            return SimpleNamespace(exit_code=self.code, stdout="", stderr="fatal: x")

    class Files:
        async def read(self, path: str, format: str = "text") -> bytes:
            return b"B"

    async def go() -> None:
        cmds = Cmds(0)
        await export_bundle(SimpleNamespace(commands=cmds, files=Files()), "/w", "")
        assert any("--all" in c for c in cmds.calls)

        with pytest.raises(BundleExportError):
            await export_bundle(
                SimpleNamespace(commands=Cmds(128), files=Files()), "/w", "main"
            )

    asyncio.run(go())


def test_push_bundle_rejects_empty_inputs() -> None:
    from edith_backend.internal.gitops.push import PushError, push_bundle

    with pytest.raises(PushError):
        asyncio.run(push_bundle(b"", "https://github.com/o/r.git", "main", "t"))
    with pytest.raises(PushError):
        asyncio.run(push_bundle(b"x", "https://github.com/o/r.git", "main", ""))
    with pytest.raises(PushError):
        asyncio.run(push_bundle(b"x", "https://github.com/o/r.git", "", "t"))


@pytest.mark.parametrize("branch", ["main", "master"])
def test_push_bundle_end_to_end_with_local_bare_remote(tmp_path, branch: str) -> None:
    """D-2 全链路：bundle → 后端 fetch → push 到远端。

    用**本地裸仓库**当远端（SSRF 校验在 HTTP 层，本函数不做），故无需真实凭据。
    参数化覆盖「分支名与 `git init` 默认分支同名」的回归——曾导致
    `refusing to fetch into branch 'refs/heads/master' checked out at ...`。
    """
    import subprocess

    from edith_backend.internal.gitops.push import push_bundle

    def git(*args: str, cwd: str | None = None) -> str:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )
        return done.stdout

    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))

    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", cwd=str(src))
    git("config", "user.email", "t@example.com", cwd=str(src))
    git("config", "user.name", "Tester", cwd=str(src))
    (src / "a.txt").write_text("hello from sandbox\n", encoding="utf-8")
    git("add", "a.txt", cwd=str(src))
    git("commit", "-q", "-m", "first commit", cwd=str(src))
    git("branch", "-M", branch, cwd=str(src))

    bundle_path = tmp_path / "out.bundle"
    git("bundle", "create", str(bundle_path), "--all", cwd=str(src))

    result = asyncio.run(
        push_bundle(bundle_path.read_bytes(), str(remote), branch, "dummy-token")
    )
    assert result["branch"] == branch
    assert "first commit" in git("log", "--oneline", branch, cwd=str(remote))


def _init_repo(path, *, branch: str = "master", file_name: str = "a.txt", content: str = "x\n"):
    """建一个只有一次提交的最小仓库，返回 path。"""
    import subprocess

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=str(path), capture_output=True, text=True, check=True
        )
        return done.stdout

    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    (path / file_name).write_text(content, encoding="utf-8")
    git("add", file_name)
    git("commit", "-q", "-m", f"commit {content.strip()}")
    git("branch", "-M", branch)
    return path


def test_push_bundle_target_branch_differs_from_source(tmp_path) -> None:
    """本地 master → 远端 main：源/目标分支必须能不同名。

    真实事故：refspec 写成 `x:x` 时，任何「本地分支名 ≠ 远端分支名」的场景
    （master → main、master → docs/readme）都推不上去，模型只能劝用户手工 push。
    """
    import subprocess

    from edith_backend.internal.gitops.push import push_bundle

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    src = _init_repo(tmp_path / "src", branch="master")
    bundle_path = tmp_path / "out.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle_path), "master"],
        cwd=str(src), capture_output=True, check=True,
    )

    result = asyncio.run(
        push_bundle(
            bundle_path.read_bytes(), str(remote), "master", "t",
            target_branch="main",
        )
    )
    assert result["branch"] == "master"
    assert result["remoteBranch"] == "main"

    # 远端只应出现 main，不该顺手造一个 master
    heads = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd=str(remote), capture_output=True, text=True, check=True,
    ).stdout.split()
    assert heads == ["main"]


def test_push_bundle_reports_non_fast_forward(tmp_path) -> None:
    """远端已有本地没有的提交时，必须抛 PushNonFastForward（而不是含糊的 push 失败）。"""
    import subprocess

    from edith_backend.internal.gitops.push import PushNonFastForward, push_bundle

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    # 远端 main 先有一个独立历史
    other = _init_repo(tmp_path / "other", branch="main", file_name="b.txt", content="remote\n")
    subprocess.run(["git", "push", "-q", str(remote), "main"], cwd=str(other), check=True)

    # 本地 master 是另一条历史
    src = _init_repo(tmp_path / "src", branch="master")
    bundle_path = tmp_path / "out.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle_path), "master"],
        cwd=str(src), capture_output=True, check=True,
    )

    with pytest.raises(PushNonFastForward):
        asyncio.run(
            push_bundle(
                bundle_path.read_bytes(), str(remote), "master", "t",
                target_branch="main",
            )
        )


def test_list_remote_branches(tmp_path) -> None:
    """远端查询：给出分支列表与默认分支，供模型推送前看清现状。"""
    import subprocess

    from edith_backend.internal.gitops.push import list_remote_branches

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/master"],
        cwd=str(remote), check=True,
    )
    src = _init_repo(tmp_path / "src", branch="master")
    subprocess.run(["git", "push", "-q", str(remote), "master"], cwd=str(src), check=True)
    subprocess.run(
        ["git", "push", "-q", str(remote), "master:refs/heads/docs/readme"],
        cwd=str(src), check=True,
    )

    info = asyncio.run(list_remote_branches(str(remote), ""))
    names = sorted(b["name"] for b in info["branches"])
    assert names == ["docs/readme", "master"]
    assert info["defaultBranch"] == "master"
    assert all(len(b["sha"]) == 40 for b in info["branches"])


@pytest.mark.parametrize(
    "bad", ["", "-x", "a..b", "a b", "a:b", "a*b", "/x", "x/", ".x", "x.", "a@{b"]
)
def test_validate_branch_name_rejects(bad: str) -> None:
    """分支名来自模型参数，必须挡在拼命令之前（`-x` 会被 git 当选项）。"""
    from edith_backend.internal.gitops.push import PushError, validate_branch_name

    with pytest.raises(PushError):
        validate_branch_name(bad)


@pytest.mark.parametrize("good", ["main", "master", "docs/readme-1", "feat/x_y"])
def test_validate_branch_name_accepts(good: str) -> None:
    from edith_backend.internal.gitops.push import validate_branch_name

    assert validate_branch_name(good) == good
