"""配置聚合：从各功能模块加载一次运行所需的配置。

对应 Go 版 internal/agentrun/configuration.go。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from ..models.catalog import DEFAULT_MODEL_ID, Catalog, ModelDefinition
from ..skills.catalog import Catalog as SkillsCatalog
from ..skills.types import CUSTOM_PATH, SYSTEM_PATH, SkillSummary
from ..trace import PREPARE, Tracer
from ..userconfig.mcp_runtime import MCPRuntime
from ..userconfig.providers import Providers
from ..userconfig.settings import Settings
from ..workspace.memory_store import WorkspaceMemoryStore
from .types import Error, Request

# 与 Go 版保持一致的系统身份前缀
_BASE_IDENTITY = (
    "你是 EDITH AI Agent智能助手。请全程使用中文推理和回答，包括你的内部思考过程。\n\n"
)


@dataclass
class ConfiguredRun:
    """已经可以交给执行层的完整运行输入。"""

    request: Request
    definition: ModelDefinition
    api_key: str
    system_instruction: str
    image_urls: list[str]
    message: str
    # mcp_tools: MCPToolContext | None = None


class RunConfigurations:
    """聚合模型、用户设置、供应商凭据、Skill 摘要、图片与上传文件。"""

    def __init__(
        self,
        models: Catalog,
        settings: Settings,
        providers: Providers,
        skills: SkillsCatalog | None = None,
        images=None,
        files=None,
        mcp: MCPRuntime | None = None,
        memories: WorkspaceMemoryStore | None = None,
    ) -> None:
        self.models = models
        self.settings = settings
        self.providers = providers
        self.skills = skills
        self.images = images
        self.files = files
        self.mcp = mcp
        self.memories = memories

    async def load(self, request: Request, tracer: Tracer) -> ConfiguredRun | Error:
        # 每个阶段一个 prepare 子步骤：准备阶段曾经是整体黑盒，
        # 「点了发送半天没反应」时无法区分是这一步慢还是模型慢。
        # 1. 解析模型：请求未指定则用用户默认，再兜底平台默认
        async with tracer.step(PREPARE, "model.resolve") as model_step:
            # 优先级：请求显式指定 > 用户默认 > 平台默认。
            # 后两者是「可能失效的引用」——用户删掉自己加的自定义模型后，默认值会
            # 指向一个已不存在的 ID；显式指定则不做回退（用户选的就是它，报错更诚实）。
            explicit = bool(request.model_id)
            if not request.model_id:
                request.model_id = await self.settings.load_default_model_id(
                    request.user_id
                )
            if not request.model_id:
                request.model_id = DEFAULT_MODEL_ID

            definition = await self.models.find_for(request.user_id, request.model_id)
            if definition is None and not explicit and request.model_id != DEFAULT_MODEL_ID:
                request.model_id = DEFAULT_MODEL_ID
                definition = await self.models.find_for(
                    request.user_id, request.model_id
                )
            if definition is None:
                return Error("invalid_request", "unsupported modelId")

            # 2. 图片输入需模型支持视觉
            if request.image_ids and not definition.vision:
                return Error(
                    "invalid_request", "selected model does not support image input"
                )
            model_step.metadata["model"] = definition.id

        # 3. 加载供应商密钥
        try:
            async with tracer.step(PREPARE, "provider.api_key"):
                api_key = await self.providers.load_api_key(
                    request.user_id, definition.provider_id
                )
        except LookupError as exc:
            return Error("invalid_request", f"load model credential: {exc}")

        # 4. 加载人格提示词
        async with tracer.step(PREPARE, "settings.personality"):
            personality = await self.settings.load_personality(request.user_id)

        # 5. 校验上传文件（uploads/ 目录内已存在的普通文件）
        uploads: list[str] = []
        if request.upload_paths and self.files is not None:
            try:
                async with tracer.step(PREPARE, "uploads.validate"):
                    uploads = await self.files.validate_uploads(
                        request.user_id, request.session_id, request.upload_paths
                    )
            except Exception as exc:  # noqa: BLE001 - 文件校验失败统一转 invalid_request
                return Error("invalid_request", f"prepare uploaded files: {exc}")

        # 6. 加载 Skill 摘要（公共 + 用户）
        system_summaries: list[SkillSummary] = []
        user_overview = ""
        if self.skills is not None:
            async with tracer.step(PREPARE, "skills.summaries") as skills_step:
                system_summaries = self.skills.list_system_summaries()
                user_overview = await self.skills.read_user_overview(request.user_id)
                skills_step.metadata["system"] = len(system_summaries)

        # 6.5 加载工作区记忆（指令 + 自动记忆）
        workspace_memory = ""
        if request.workspace_id and self.memories is not None:
            async with tracer.step(PREPARE, "workspace.memory") as memory_step:
                workspace_memory = await self.memories.load_injectable(
                    request.user_id, request.workspace_id
                )
                memory_step.metadata["chars"] = len(workspace_memory)

        system_instruction = _BASE_IDENTITY + personality
        if workspace_memory:
            system_instruction += "\n\n【项目背景】\n" + workspace_memory
        instruction = run_instruction(system_summaries, user_overview, uploads)

        # 7. 把图片 ID 转为短期 URL
        image_urls: list[str] = []
        if request.image_ids and self.images is not None:
            try:
                async with tracer.step(PREPARE, "images.urls") as image_step:
                    image_urls = await self.images.message_image_urls(
                        request.user_id, request.session_id, request.image_ids
                    )
                    image_step.metadata["images"] = len(image_urls)
            except Exception as exc:  # noqa: BLE001 - 图片加载失败统一转 invalid_request
                return Error("invalid_request", f"load image input: {exc}")

        # 8. 拼装最终 system prompt
        # 本次修改：删除下方对 system_instruction 的重复整体赋值（曾覆盖掉第 6.5 步注入的
        # 【项目背景】/workspace_memory，属既有 bug），改为只追加 instruction 与工作区上下文。
        if instruction:
            system_instruction += "\n\n" + instruction

        # 9. 注入当前会话工作区上下文（配合工具层自动补全，模型无需拼 workspace_id）
        # 本次新增：Web 场景模型无「当前目录」概念，显式告知其文件操作基座，减少误判。
        if request.workspace_id:
            system_instruction += (
                "\n\n【当前会话工作区】\n"
                f"当前会话关联工作区 ID：{request.workspace_id}。\n"
                "你的文件操作基于该工作区（Sandbox 内路径 workspace/{workspace_id}/...）。"
                "调用 Sandbox 文件工具时无需关心路径前缀，直接传相对路径"
                "（如 README.md、src/main.py）即可，工具会自动定位到该工作区。"
            )

        # 8. 打开用户启用的 MCP 服务工具
        # mcp_tools: MCPToolContext | None = None
        # if self.mcp is not None:
        #     mcp_tools = await self.mcp.open_tools(request.user_id)

        return ConfiguredRun(
            request=request,
            definition=definition,
            api_key=api_key,
            system_instruction=system_instruction,
            image_urls=image_urls,
            message=message_with_uploads(request.message, uploads),
            # mcp_tools=mcp_tools,
        )


def message_with_uploads(content: str, uploads: list[str]) -> str:
    """把附件名称写入用户消息，保证会话历史也能展示本次上传。"""
    content = content.strip()
    if not uploads:
        return content
    files = [f"- `{posixpath.basename(upload)}`" for upload in uploads]
    attachments = "附件：\n" + "\n".join(files)
    if not content:
        return attachments
    return content + "\n\n" + attachments


def run_instruction(
    summaries: list[SkillSummary], overview: str, uploads: list[str]
) -> str:
    """把 Skill 摘要与上传文件路径拼成一次运行的 Instruction。"""
    instruction = skill_instruction(summaries, overview)
    if not uploads:
        return instruction
    files = (
        "本次用户上传文件：\n- "
        + "\n- ".join(uploads)
        + "\n请通过 Sandbox 文件工具从 uploads/ 读取这些文件。"
    )
    if not instruction:
        return files
    return instruction + "\n\n" + files


def skill_instruction(summaries: list[SkillSummary], user_overview: str) -> str:
    """把公共和用户 Skill 摘要拼成一次运行的 Instruction。"""
    if not summaries and not user_overview.strip():
        return ""
    lines: list[str] = []
    if summaries:
        lines.append("可用公共 Skills：")
        for summary in summaries:
            lines.append(f"- {summary.name}：{summary.description}")
    overview = user_overview.strip()
    if overview:
        if lines:
            lines.append("")
        lines.append("可用用户 Skills：")
        lines.append(overview)
    lines.append(
        f"完整 Skill 文件和资源位于 Sandbox 工作区：公共 Skills {SYSTEM_PATH}/<skill-name>/，"
        f"用户 Skills {CUSTOM_PATH}/<skill-name>/。需要完整规则或资源时，通过 Sandbox 文件工具读取对应目录。"
    )
    return "\n".join(lines)
