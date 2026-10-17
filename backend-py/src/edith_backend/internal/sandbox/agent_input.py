"""sandbox 的 Agent 输入：校验本次运行可引用的用户上传文件。

对应 Go 版 internal/sandbox/agent_input.go。
"""

from __future__ import annotations

import posixpath

from e2b import FileType

from .service import Service
from .workspace import WORKSPACE


class AgentInput:
    def __init__(self, service: Service) -> None:
        self._service = service

    async def validate_uploads(
        self, user_id: str, session_id: str, paths: list[str]
    ) -> list[str]:
        """只接受当前用户、当前会话 uploads/ 内已存在的普通文件。"""
        if not paths:
            return []
        sandbox = await self._service.existing_workspace(user_id, session_id)
        validated: list[str] = []
        seen: set[str] = set()
        for value in paths:
            relative = value.strip().replace("\\", "/")
            if (
                not relative.startswith(WORKSPACE.uploads + "/")
                or posixpath.normpath(relative) != relative
                or ".." in relative
            ):
                raise ValueError(f"file must be inside {WORKSPACE.uploads}/")
            if relative in seen:
                continue
            info = await sandbox.files.get_info(posixpath.join(WORKSPACE.root, relative))
            if info.type == FileType.DIR:
                raise ValueError(f"uploaded file does not exist: {relative}")
            seen.add(relative)
            validated.append(relative)
        return validated
