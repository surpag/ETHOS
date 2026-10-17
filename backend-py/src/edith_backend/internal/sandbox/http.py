"""sandbox 的文件浏览、上传与下载 HTTP 接口。

对应 Go 版 internal/sandbox/http.go + routes.go。
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from fastapi import APIRouter, UploadFile
from fastapi.responses import StreamingResponse

from ..http_utils import EDITHError
from .service import SandboxNotFound, Service
from .workspace import relative_path, workspace_path

DEFAULT_READ_LIMIT = 32 * 1024
MAX_UPLOAD_SIZE = 50 << 20


def _local_entries(relative: str, directory: Path) -> list[dict]:
    """把本地目录扫成与 E2B list_files 相同的响应结构（P0 本地直读）。"""
    entries: list[dict] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return entries
    for child in children:
        entries.append(
            {
                "name": child.name,
                "path": posixpath.join(relative, child.name),
                "type": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return entries


class HTTP:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.router = self._build_router()

    async def _existing(self, user_id: str, session_id: str, path: str, allow_root: bool):
        if not user_id.strip() or not session_id.strip():
            raise EDITHError(400, "invalid_request", "userId and sessionId are required")
        try:
            real_path = workspace_path(path, allow_root)
        except ValueError as exc:
            raise EDITHError(400, "invalid_path", str(exc))
        try:
            sandbox = await self.service.existing_workspace(user_id, session_id)
        except SandboxNotFound:
            raise EDITHError(404, "sandbox_not_found", "sandbox has not been created for this session")
        return sandbox, real_path

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/sandbox/files")
        async def list_files(userId: str = "", sessionId: str = "", path: str = "") -> dict:
            # P0 优化：Local 后端下 workspace/{id}/... 直接读本地磁盘（毫秒级），绕开 E2B。
            # 但只在本地**确实存在**该目录时才直读；否则回落读沙箱——这样「克隆进沙箱、
            # 还没同步回后端」的文件也能在文件面板里看到。
            local_dir = await self.service.local_workspace_path(userId, path)
            if local_dir is not None and local_dir.is_dir():
                return {"path": path, "entries": _local_entries(path, local_dir)}
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=True)
            try:
                entries = await sandbox.files.list(real_path, depth=1)
            except Exception:  # noqa: BLE001
                # 目录不存在（如工作区 Volume 尚未挂载到当前沙箱）→ 按空目录处理，避免 500
                entries = []
            return {
                "path": relative_path(real_path),
                "entries": [
                    {"name": e.name, "path": relative_path(e.path),
                     "type": "directory" if e.type.value == "dir" else "file", "size": e.size or 0}
                    for e in entries
                ],
            }

        @router.get("/internal/sandbox/files/content")
        async def read_content(userId: str = "", sessionId: str = "", path: str = "") -> dict:
            # P0 优化：同上，但只在本地**确实存在该文件**时才直读；
            # 否则回落读沙箱（克隆进沙箱的文件不在后端磁盘上）。
            local_file = await self.service.local_workspace_path(userId, path)
            if local_file is not None and local_file.is_file():
                data = local_file.read_text(encoding="utf-8", errors="replace")
                truncated = len(data) > DEFAULT_READ_LIMIT
                content = data[:DEFAULT_READ_LIMIT]
                if "\x00" in content:
                    raise EDITHError(422, "file_not_previewable", "only UTF-8 text files can be previewed")
                return {"path": path, "content": content, "truncated": truncated}
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=False)
            data = await sandbox.files.read(real_path)
            truncated = len(data) > DEFAULT_READ_LIMIT
            content = data[:DEFAULT_READ_LIMIT]
            if "\x00" in content:
                raise EDITHError(422, "file_not_previewable", "only UTF-8 text files can be previewed")
            return {"path": relative_path(real_path), "content": content, "truncated": truncated}

        @router.post("/internal/sandbox/files/upload")
        async def upload_file(
            userId: str = "",
            sessionId: str = "",
            file: UploadFile | None = None,
        ) -> dict:
            if not userId.strip() or not sessionId.strip():
                raise EDITHError(400, "invalid_request", "userId and sessionId are required")
            if file is None or not file.filename:
                raise EDITHError(400, "invalid_upload", "file is required")
            name = file.filename.replace("\\", "/").split("/")[-1]
            if not name or name in (".", "/"):
                raise EDITHError(400, "invalid_upload", "invalid file name")
            data = await file.read()
            if not data or len(data) > MAX_UPLOAD_SIZE:
                raise EDITHError(400, "invalid_upload", "file must be non-empty and at most 50 MB")
            try:
                sandbox = await self.service.workspace(userId, sessionId)
                target = workspace_path(f"uploads/{name}", allow_root=False)
                await sandbox.files.write(target, data)
            except Exception as exc:  # noqa: BLE001
                raise EDITHError(502, "sandbox_files_unavailable", str(exc))
            return {"path": relative_path(target), "name": name, "size": len(data)}

        @router.get("/internal/sandbox/files/download")
        async def download_file(userId: str = "", sessionId: str = "", path: str = "") -> StreamingResponse:
            sandbox, real_path = await self._existing(userId, sessionId, path, allow_root=False)
            if not relative_path(real_path).startswith("artifacts/"):
                raise EDITHError(403, "download_not_allowed", "only artifacts files can be downloaded")
            data = await sandbox.files.read(real_path)
            filename = path.split("/")[-1]
            return StreamingResponse(
                iter([data]),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        return router
