"""工作区实体的 HTTP 路由（含 Volume 文件上传与级联删除）。

对应 `docs/p1-workspace-volume-design.md`：
- POST/GET /internal/workspaces + DELETE /internal/workspaces/{id}（级联删指令 + 销毁 Volume）
- POST /internal/workspaces/{id}/files（上传文件到 Volume，懒创建）
- GET/PUT/DELETE /internal/workspaces/{id}/instructions（整段覆盖，等价 AGENTS.md）
- GET/PUT/DELETE /internal/workspaces/{id}/memories（自动记忆）
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Query, UploadFile, status

from ..gitops.ssrf import CloneUrlRejected, assert_safe_clone_url
from ..http_utils import EDITHError
from .memory_store import WorkspaceMemoryStore
from .store import DuplicateWorkspace, WorkspaceStore
from .types import (
    AllowedDomainsRequest,
    AllowedDomainsResponse,
    BindLocalRootRequest,
    BindLocalRootResponse,
    GitRemoteRequest,
    GitRemoteResponse,
    InstructionRequest,
    InstructionResponse,
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceMemoryCreateRequest,
    WorkspaceMemoryCreateResponse,
    WorkspaceMemoryItem,
    WorkspaceMemoryListResponse,
    WorkspaceRenameRequest,
    WorkspaceResponse,
)
from .volume_backend import LocalVolumeBackend
from .volume_service import WorkspaceVolumeService

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 单文件 50MB（对齐现有上传）
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 单次请求总大小上限

# 沙箱出口白名单的硬限制（E2B 服务端超限直接 400 且不可提额）
MAX_ALLOWED_DOMAINS = 10

# 允许的域名形态：普通域名，或单层通配 `*.example.com`（apex 需单独写）
_DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def _normalize_domains(raw: list[str]) -> list[str]:
    """规范化并校验出口白名单：小写、去空、去重、限 10 个、格式须为域名或 *.域名。"""
    seen: dict[str, None] = {}
    for item in raw:
        domain = item.strip().lower().rstrip(".")
        if not domain:
            continue
        if not _DOMAIN_RE.match(domain):
            raise EDITHError(400, "invalid_domain", f"invalid domain: {item!r}")
        seen[domain] = None
    domains = list(seen)
    if len(domains) > MAX_ALLOWED_DOMAINS:
        raise EDITHError(
            400,
            "too_many_domains",
            f"at most {MAX_ALLOWED_DOMAINS} domains per sandbox (E2B limit)",
        )
    return domains

# 方案 B：允许绑定的本地目录白名单（os.pathsep 分隔；未配置则放行并告警）
_ALLOWED_LOCAL_ROOTS = [
    p.strip() for p in os.getenv("EDITH_WORKSPACE_LOCAL_ROOTS", "").split(os.pathsep) if p.strip()
]


def _validate_local_root(raw: str) -> str:
    """校验本地目录：必须是存在的绝对路径，且在白名单内（未配置白名单则放行并告警）。"""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise EDITHError(400, "invalid_local_root", "localRoot must be an absolute path")
    resolved = path.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise EDITHError(400, "invalid_local_root", f"localRoot not a directory: {resolved}")
    if _ALLOWED_LOCAL_ROOTS:
        allowed = [Path(p).expanduser().resolve() for p in _ALLOWED_LOCAL_ROOTS]
        if not any(resolved == a or a in resolved.parents for a in allowed):
            raise EDITHError(
                403, "local_root_not_allowed", f"localRoot outside whitelist: {resolved}"
            )
    else:
        logger.warning(
            "EDITH_WORKSPACE_LOCAL_ROOTS 未配置，放行任意本地目录绑定: %s", resolved
        )
    return str(resolved)


def _detect_local_root_prefix(
    volume_service: WorkspaceVolumeService, workspace_id: str, local_root: Path
) -> str:
    """自动探测本地目录在沙箱文件树中的前缀。

    "选文件夹上传"创建的工作区，卷里会多一层文件夹名（如 Data/），
    此时本地目录 D:\\agent\\Data 对应沙箱 workspace/{id}/Data/。

    判定：卷根下**存在**一个与本地目录同名的子目录即命中——不再要求它是卷里唯一的条目。
    早前"卷根必须只有这一个条目"的判定几乎不命中：只要 Agent 在绑定前写过任何东西
    （报告、缓存…），卷根就有多个条目，探测退化，前缀错判成 ""，整棵旧副本被原样搬平。
    """
    backend = volume_service.backend
    if not isinstance(backend, LocalVolumeBackend):
        return ""
    legacy = backend.legacy_dir(workspace_id)
    if not legacy.is_dir():
        return ""
    try:
        matches = [
            entry
            for entry in legacy.iterdir()
            if entry.is_dir() and entry.name == local_root.name
        ]
    except OSError:
        return ""
    return matches[0].name if len(matches) == 1 else ""


def build_workspace_router(
    store: WorkspaceStore, volume_service: WorkspaceVolumeService
) -> APIRouter:
    """工作区实体路由（含级联删除与文件上传）。"""
    router = APIRouter()

    @router.post("/internal/workspaces", response_model=WorkspaceResponse)
    async def create_workspace(req: WorkspaceCreateRequest):
        name = req.name.strip()
        local_root = _validate_local_root(req.local_root) if req.local_root else None
        try:
            workspace_id = await store.create(
                req.user_id, name, req.metadata, local_root, req.session_id
            )
        except DuplicateWorkspace as exc:
            raise EDITHError(409, "workspace_exists", str(exc)) from exc
        # 绑定本地目录到后端（B1a：让后续读写直连用户目录）
        if local_root:
            await volume_service.load_and_bind_local_root(req.user_id, workspace_id)
        return WorkspaceResponse(
            workspace_id=workspace_id,
            name=name,
            metadata=req.metadata or {},
            local_root=local_root or "",
            allowed_domains=(req.metadata or {}).get("allowedDomains") or [],
            git_remote_url=(req.metadata or {}).get("gitRemoteUrl") or "",
        )

    @router.get("/internal/workspaces", response_model=WorkspaceListResponse)
    async def list_workspaces(
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        rows = await store.list(user_id)
        return WorkspaceListResponse(
            workspaces=[
                WorkspaceResponse(
                    workspace_id=row["workspaceId"],
                    name=row["name"],
                    metadata=row["metadata"],
                    has_volume=row["hasVolume"],
                    local_root=row["localRoot"],
                    allowed_domains=row["metadata"].get("allowedDomains") or [],
                    git_remote_url=row["metadata"].get("gitRemoteUrl") or "",
                )
                for row in rows
            ]
        )

    @router.delete(
        "/internal/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def delete_workspace(
        workspace_id: str,
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        deleted = await store.delete(
            user_id, workspace_id, on_destroy_volume=volume_service.on_delete
        )
        if not deleted:
            raise EDITHError(404, "not_found", "workspace not found")

    @router.patch(
        "/internal/workspaces/{workspace_id}", response_model=WorkspaceResponse
    )
    async def rename_workspace(
        workspace_id: str,
        req: WorkspaceRenameRequest,
    ):
        """本次新增：重命名工作区。返回更新后的工作区（含 has_volume）。"""
        name = req.name.strip()
        if not name:
            raise EDITHError(400, "invalid_request", "name is required")
        updated = await store.update_name(req.user_id, workspace_id, name)
        if not updated:
            raise EDITHError(404, "not_found", "workspace not found")
        record = await store.get(req.user_id, workspace_id)
        volume = await store.load_volume(req.user_id, workspace_id)
        if record is None:
            raise EDITHError(404, "not_found", "workspace not found")
        return WorkspaceResponse(
            workspace_id=workspace_id,
            name=name,
            metadata=record["metadata"],
            has_volume=volume is not None,
            local_root=record.get("localRoot", "") or "",
            allowed_domains=record["metadata"].get("allowedDomains") or [],
            git_remote_url=record["metadata"].get("gitRemoteUrl") or "",
        )

    @router.post(
        "/internal/workspaces/{workspace_id}/bind-local-root",
        response_model=BindLocalRootResponse,
    )
    async def bind_local_root(workspace_id: str, req: BindLocalRootRequest):
        """方案 B：给已有工作区绑定本地目录（读写直连，无需重建工作区）。

        - 白名单校验（EDITH_WORKSPACE_LOCAL_ROOTS）
        - 自动探测前缀（卷里多一层文件夹名时，如 Data/）
        - 存量文件**默认不合并**；req.merge_existing=True 时才把旧副本中本地尚不存在的
          文件「只新增不覆盖」地合并过去。响应回报未合并数量供 UI 提示。
        """
        record = await store.get(req.user_id, workspace_id)
        if record is None:
            raise EDITHError(404, "not_found", "workspace not found")
        local_root = _validate_local_root(req.local_root)
        if req.local_root_prefix is None:
            prefix = _detect_local_root_prefix(
                volume_service, workspace_id, Path(local_root)
            )
        else:
            prefix = req.local_root_prefix.strip().strip("/")
        outcome = await volume_service.bind_local_root(
            req.user_id,
            workspace_id,
            local_root,
            prefix,
            merge_existing=req.merge_existing,
        )
        logger.info(
            "workspace %s bound to local root %s "
            "(prefix=%r, merge_existing=%s, merged_files=%d, unmerged_files=%d)",
            workspace_id,
            local_root,
            prefix,
            req.merge_existing,
            outcome.merged_files,
            outcome.unmerged_files,
        )
        return BindLocalRootResponse(
            workspace_id=workspace_id,
            local_root=local_root,
            local_root_prefix=prefix,
            merged_files=outcome.merged_files,
            unmerged_files=outcome.unmerged_files,
        )

    @router.put(
        "/internal/workspaces/{workspace_id}/allowed-domains",
        response_model=AllowedDomainsResponse,
    )
    async def set_allowed_domains(
        workspace_id: str, req: AllowedDomainsRequest
    ):
        """设置/清除沙箱出口白名单（存于 metadata.allowedDomains）。

        生效时机：下一次沙箱连接——SandboxService 每次连接都会核对并下发策略
        （update_network 整体替换）。domains 为空 = 清除白名单（恢复全放通）。
        """
        record = await store.get(req.user_id, workspace_id)
        if record is None:
            raise EDITHError(404, "not_found", "workspace not found")
        domains = _normalize_domains(req.domains)
        updated = await store.update_metadata(
            req.user_id, workspace_id, {"allowedDomains": domains}
        )
        if updated is None:
            raise EDITHError(404, "not_found", "workspace not found")
        logger.info(
            "workspace %s egress allowlist set to %d domains: %s",
            workspace_id,
            len(domains),
            domains,
        )
        return AllowedDomainsResponse(workspace_id=workspace_id, domains=domains)

    @router.put(
        "/internal/workspaces/{workspace_id}/git-remote",
        response_model=GitRemoteResponse,
    )
    async def set_git_remote(workspace_id: str, req: GitRemoteRequest):
        """绑定/解绑工作区的 Git 远端仓库地址（存于 metadata.gitRemoteUrl）。

        remoteUrl 为空 = 解绑。非空时做 SSRF 校验：只接受公网 https/ssh，
        内网与云元数据端点被拒绝。
        """
        remote = req.remote_url.strip()
        if remote:
            try:
                assert_safe_clone_url(remote)
            except CloneUrlRejected as exc:
                raise EDITHError(400, "invalid_remote", str(exc)) from exc
        updated = await store.update_metadata(
            req.user_id, workspace_id, {"gitRemoteUrl": remote}
        )
        if updated is None:
            raise EDITHError(404, "not_found", "workspace not found")
        logger.info(
            "workspace %s git remote set to %s", workspace_id, remote or "(cleared)"
        )
        return GitRemoteResponse(workspace_id=workspace_id, remote_url=remote)

    @router.post(
        "/internal/workspaces/{workspace_id}/files",
        response_model=dict,
        status_code=status.HTTP_200_OK,
    )
    async def upload_workspace_files(
        workspace_id: str,
        user_id: str = Query(..., alias="userId", min_length=1),
        files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI 多文件上传标准用法
    ):
        """上传文件到工作区 Volume（multipart，字段名 file，文件名即相对路径）。

        文件相对路径约定：文件名必须是 Volume 内相对路径（如 src/main.py），
        不能是绝对路径或包含 '..'。单文件 ≤50MB，单次请求 ≤200MB。
        """
        if not files:
            raise EDITHError(400, "invalid_request", "no files provided")

        # 校验工作区归属
        workspace = await store.get(user_id, workspace_id)
        if workspace is None:
            raise EDITHError(404, "not_found", "workspace not found")

        # 读取并校验文件
        file_data: list[tuple[str, bytes]] = []
        total = 0
        for upload_file in files:
            rel_path = (upload_file.filename or "").replace("\\", "/").strip()
            if not volume_service.is_safe_rel_path(rel_path):
                raise EDITHError(400, "invalid_path", f"unsafe path: {rel_path}")
            content = await upload_file.read()
            if len(content) > MAX_FILE_BYTES:
                raise EDITHError(400, "file_too_large", f"file too large: {rel_path}")
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise EDITHError(400, "total_too_large", "total size exceeds 200MB")
            file_data.append((rel_path, content))

        try:
            written = await volume_service.write_files(user_id, workspace_id, file_data)
        except Exception as exc:
            message = str(exc)
            logger.error("failed to write files to volume: %s", exc)
            # E2B 403：Volumes 未启用 → 明确提示，便于排错
            if "403" in message and "volumes" in message.lower():
                raise EDITHError(
                    502,
                    "volume_not_enabled",
                    "E2B Sandbox Volumes 未启用：请检查账号套餐（e2b.dev），或设置 "
                    "EDITH_WORKSPACE_VOLUME_BACKEND=local 使用本地存储模式",
                ) from exc
            raise EDITHError(502, "volume_unavailable", "failed to upload files") from exc

        return {"uploaded": written, "fileCount": written, "totalSize": total}

    return router


def build_instruction_router(store: WorkspaceMemoryStore) -> APIRouter:
    """工作区指令路由（整段覆盖式保存）。"""
    router = APIRouter()

    @router.get(
        "/internal/workspaces/{workspace_id}/instructions", response_model=InstructionResponse
    )
    async def get_instruction(
        workspace_id: str,
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        content = await store.load_instruction(user_id, workspace_id)
        return InstructionResponse(workspace_id=workspace_id, content=content)

    @router.put(
        "/internal/workspaces/{workspace_id}/instructions", response_model=InstructionResponse
    )
    async def save_instruction(
        workspace_id: str,
        req: InstructionRequest,
    ):
        content = req.content.strip()
        await store.save_instruction(req.user_id, workspace_id, content)
        return InstructionResponse(workspace_id=workspace_id, content=content)

    @router.delete(
        "/internal/workspaces/{workspace_id}/instructions",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def clear_instruction(
        workspace_id: str,
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        await store.save_instruction(user_id, workspace_id, "")

    return router


def build_memory_router(store: WorkspaceMemoryStore) -> APIRouter:
    """自动记忆路由（kind='auto'，Phase B 由 AI 写入；前端可见可删）。"""
    router = APIRouter()

    @router.get(
        "/internal/workspaces/{workspace_id}/memories", response_model=WorkspaceMemoryListResponse
    )
    async def list_memories(
        workspace_id: str,
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        rows = await store.list(user_id, workspace_id)
        return WorkspaceMemoryListResponse(
            memories=[WorkspaceMemoryItem(**row) for row in rows]
        )

    @router.put(
        "/internal/workspaces/{workspace_id}/memories",
        response_model=WorkspaceMemoryCreateResponse,
    )
    async def create_memory(
        workspace_id: str,
        req: WorkspaceMemoryCreateRequest,
    ):
        new_id = await store.add(req.user_id, workspace_id, req.content)
        return WorkspaceMemoryCreateResponse(id=new_id, content=req.content)

    @router.delete(
        "/internal/workspaces/{workspace_id}/memories/{memory_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_memory(
        workspace_id: str,
        memory_id: int,
        user_id: str = Query(..., alias="userId", min_length=1),
    ):
        await store.delete(memory_id, user_id)

    return router
