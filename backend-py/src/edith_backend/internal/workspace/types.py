"""workspace 模块的类型定义（HTTP 契约，camelCase 对齐 Go 版 json tag）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ===== 工作区实体（Workspace）=====


class WorkspaceCreateRequest(BaseModel):
    """POST /internal/workspaces 的请求体"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=50)
    metadata: dict | None = None
    # 方案 B：绑定本地目录（后端白名单校验）
    local_root: str | None = None
    # 决策 1：绑定会话（一个 workspace 只允许一个 session）
    session_id: str | None = None


class WorkspaceRenameRequest(BaseModel):
    """PATCH /internal/workspaces/{id} 的请求体（本次新增：重命名）"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=50)


class BindLocalRootRequest(BaseModel):
    """POST /internal/workspaces/{id}/bind-local-root 的请求体（方案 B：给已有工作区绑定本地目录）"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    local_root: str = Field(..., min_length=1)
    # 可选：显式指定前缀（如卷里多一层 Data/ 时填 "Data"）；留空则自动探测
    local_root_prefix: str | None = None
    # 是否把旧副本（未绑定期间的产物）中本地尚不存在的文件合并过来。
    # 默认 False：绑定一个既有目录不应附带全量文件迁移；需要旧产物时由用户显式勾选
    # （仍只新增不覆盖）。未合并的数量在响应 unmerged_files 中回报。
    merge_existing: bool = False


class BindLocalRootResponse(BaseModel):
    """绑定结果：绑定的目录、前缀，以及存量文件的合并情况。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspace_id: str
    local_root: str
    local_root_prefix: str = ""
    # 本次从旧副本合并过来的文件数（未勾选合并时为 0）
    merged_files: int = 0
    # 旧副本中仍未合并（本地缺失）的文件数；>0 时提示用户可勾选「合并存量文件」
    unmerged_files: int = 0


class AllowedDomainsRequest(BaseModel):
    """PUT /internal/workspaces/{id}/allowed-domains 的请求体（沙箱出口白名单）。

    domains 为空 = 清除白名单（恢复全放通）。E2B 硬限制：每沙箱最多 10 个域名。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    domains: list[str] = Field(default_factory=list, max_length=10)


class AllowedDomainsResponse(BaseModel):
    """出口白名单当前生效值。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspace_id: str
    domains: list[str] = Field(default_factory=list)


class GitRemoteRequest(BaseModel):
    """PUT /internal/workspaces/{id}/git-remote 的请求体。

    绑定工作区的 Git 远端仓库地址（https/ssh 公网地址）；空串 = 解绑。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    remote_url: str = ""


class GitRemoteResponse(BaseModel):
    """当前绑定的 Git 远端地址。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspace_id: str
    remote_url: str = ""


class WorkspaceResponse(BaseModel):
    """工作区响应（只暴露名称，workspace_id 由系统管理）"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspace_id: str
    name: str
    metadata: dict = Field(default_factory=dict)
    has_volume: bool = False
    # 方案 B：绑定的本地目录（空 = 未绑定）
    local_root: str = ""
    # 沙箱出口白名单（空 = 全放通）；存于 metadata.allowedDomains
    allowed_domains: list[str] = Field(default_factory=list)
    # D-a：绑定的 Git 远端仓库地址（空 = 未绑定）；存于 metadata.gitRemoteUrl
    git_remote_url: str = ""


class WorkspaceListResponse(BaseModel):
    """GET /internal/workspaces 的响应"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspaces: list[WorkspaceResponse]


class InstructionRequest(BaseModel):
    """PUT /internal/workspaces/{id}/instructions 的请求体（整段覆盖）"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    content: str = ""


class InstructionResponse(BaseModel):
    """GET /internal/workspaces/{id}/instructions 的响应"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    workspace_id: str
    content: str = ""


# ===== 自动记忆（Workspace Memory）=====


class WorkspaceMemoryItem(BaseModel):
    """单条自动记忆实体（用于列表项和响应）"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    content: str


class WorkspaceMemoryListResponse(BaseModel):
    """GET /internal/workspaces/{id}/memories 的响应"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    memories: list[WorkspaceMemoryItem]


class WorkspaceMemoryCreateRequest(BaseModel):
    """PUT /internal/workspaces/{id}/memories 的请求体"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, max_length=500)


class WorkspaceMemoryCreateResponse(BaseModel):
    """PUT /internal/workspaces/{id}/memories 的响应"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: int
    content: str
