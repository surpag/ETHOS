"""配置聚合：从各功能模块加载一次运行所需的配置。

对应 Go 版 internal/agentrun/configuration.go。
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field

from ..models.catalog import DEFAULT_MODEL_ID, Catalog, ModelDefinition
from ..session.constraint_store import (
    ConstraintStore,
    constraint_budget_chars,
    constraints_enabled,
)
from ..skills.budget import (
    MAX_SKILL_BYTES_PER_RUN,
    MAX_SKILLS_PER_RUN,
    index_budget_chars,
    plan_index,
)
from ..skills.catalog import Catalog as SkillsCatalog
from ..skills.resolver import (
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    SCOPE_USER,
    ResolvedCatalog,
    ScopeDecision,
    SkillResolver,
)
from ..skills.storage import STATUS_UNAVAILABLE as SKILL_STATUS_UNAVAILABLE
from ..skills.storage import SkillScopeStores
from ..skills.types import CUSTOM_PATH, PROJECT_PATH, SYSTEM_PATH
from ..trace import PREPARE, Tracer
from ..userconfig.mcp_runtime import MCPRuntime
from ..userconfig.providers import Providers
from ..userconfig.settings import Settings
from ..workspace.memory_store import WorkspaceMemoryStore
from .types import Error, Request

logger = logging.getLogger(__name__)


def _decision_view(decision: ScopeDecision) -> dict:
    """把一条裁决记录压成 trace 友好的小字典（字段名短，便于前端直接展示）。

    刻意不塞整份 `ScopeDecision`：trace metadata 会被写进 JSON 列并在前端渲染，
    多一个字段就多一处「前端要跟着改」的耦合。
    """
    view = {
        "name": decision.name,
        "winner": decision.winner_scope,
        "loser": decision.loser_scope,
        "reason": decision.reason,
    }
    if decision.detail:
        view["detail"] = decision.detail
    return view

# 与 Go 版保持一致的系统身份前缀
_BASE_IDENTITY = (
    "你是 EDITH AI Agent智能助手。请全程使用中文推理和回答，包括你的内部思考过程。\n"
    # 计划纪律：只说「先列计划」不够——必须同时说清「标完成要能被核对」，
    # 否则模型会把计划当成另一种更可信的自述（打勾比说话更像证据，但同样可能没做）。
    "多步任务（超过 3 步）先用 plan_update 建立计划，之后随进展更新；"
    "把某一项标为 done 时必须给出可核对的证据（改动的文件路径，或命令的退出码），"
    "没做成不要打勾。\n\n"
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
        constraints: ConstraintStore | None = None,
        skill_scopes: SkillScopeStores | None = None,
    ) -> None:
        self.models = models
        self.settings = settings
        self.providers = providers
        self.skills = skills
        # Skill 的「有哪些生效」只由解析器回答，注入侧不再自己去两个来源拼接。
        # 解析器还需要作用域分发点才能读项目层；缺它时项目层按「未配置」上报，
        # 而不是让整个解析失败（纯聊天会话本来就没有项目级）。
        self.skill_resolver = (
            SkillResolver(skills, skill_scopes) if skills is not None else None
        )
        self.images = images
        self.files = files
        self.mcp = mcp
        self.memories = memories
        self.constraints = constraints

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

        # 6. 解析 Skill 作用域（平台 + 用户 + 有工作区时的项目）
        resolved_skills = ResolvedCatalog()
        if self.skill_resolver is not None:
            async with tracer.step(PREPARE, "skills.summaries") as skills_step:
                resolved_skills = await self.skill_resolver.resolve(
                    request.user_id, request.workspace_id
                )
                platform_report = resolved_skills.report(SCOPE_PLATFORM)
                user_report = resolved_skills.report(SCOPE_USER)
                project_report = resolved_skills.report(SCOPE_PROJECT)
                # 各层数量与状态必须落 trace：旧实现只记 system 数量，
                # 于是「用户 Skill 一个都没注入」这件事在 trace 里完全看不出来。
                skills_step.metadata["system"] = platform_report.count
                # 用解析出的条数而不是存储层给的 summaries 长度：e2b 后端只给索引原文，
                # summaries 为空，按旧口径这里会记 0，而实际注入的文本里是有条目的。
                skills_step.metadata["user"] = user_report.count
                skills_step.metadata["userStatus"] = user_report.status
                if user_report.reason:
                    skills_step.metadata["userReason"] = user_report.reason
                if user_report.healed:
                    skills_step.metadata["userHealed"] = True
                # 项目级三项独立记：只记一个"总计"就无法回答「这个项目到底有没有 Skill」
                skills_step.metadata["project"] = project_report.count
                skills_step.metadata["projectStatus"] = project_report.status
                if project_report.reason:
                    skills_step.metadata["projectReason"] = project_report.reason
                if project_report.healed:
                    skills_step.metadata["projectHealed"] = True
                # 覆盖关系只走这里（不落库）：它是"这次 Discovery 怎么裁的"，
                # 不是"谁永久被禁用"——下一轮重新算，删掉项目级就自动恢复（D3）。
                if resolved_skills.shadows:
                    skills_step.metadata["shadows"] = [
                        _decision_view(item) for item in resolved_skills.shadows
                    ]
                if resolved_skills.conflicts:
                    skills_step.metadata["conflicts"] = [
                        _decision_view(item) for item in resolved_skills.conflicts
                    ]
                if user_report.status == SKILL_STATUS_UNAVAILABLE:
                    logger.warning(
                        "user skills unavailable user=%s reason=%s",
                        request.user_id[:12],
                        user_report.reason,
                    )
                if project_report.status == SKILL_STATUS_UNAVAILABLE:
                    # 项目级读不到必须留痕并带 workspace：多项目并发时否则无法归因
                    logger.warning(
                        "project skills unavailable user=%s ws=%s reason=%s",
                        request.user_id[:12],
                        request.workspace_id[:12],
                        project_report.reason,
                    )

        # 6.5 加载工作区记忆（指令 + 自动记忆）
        workspace_memory = ""
        if request.workspace_id and self.memories is not None:
            async with tracer.step(PREPARE, "workspace.memory") as memory_step:
                workspace_memory = await self.memories.load_injectable(
                    request.user_id, request.workspace_id
                )
                memory_step.metadata["chars"] = len(workspace_memory)

        system_instruction = _BASE_IDENTITY + personality
        # 6.6 约束层：历史之外的一层，永不压缩。
        # 位置刻意靠前且独立成段——它和【项目背景】的性质不同：项目背景是「背景信息」，
        # 约束是「不许违反的边界」。混在一起写会让模型把硬约束当参考资料。
        if constraints_enabled() and self.constraints is not None:
            async with tracer.step(PREPARE, "constraints.inject") as constraint_step:
                constraint_block, injected, omitted = await self.constraints.render_for_prompt(
                    request.user_id,
                    session_id=request.session_id,
                    workspace_id=request.workspace_id,
                    budget_chars=constraint_budget_chars(),
                )
                constraint_step.metadata["injected"] = injected
                constraint_step.metadata["omitted"] = omitted
            if constraint_block:
                system_instruction += "\n\n" + constraint_block
            if omitted:
                # 绝不静默丢弃：预算截断必须可见，否则「约束没生效」无法归因。
                logger.warning(
                    "constraints_truncated user=%s session=%s injected=%d omitted=%d",
                    request.user_id,
                    request.session_id,
                    injected,
                    omitted,
                )

        if workspace_memory:
            system_instruction += "\n\n【项目背景】\n" + workspace_memory
        # 6.7 注入渲染：**唯一**的 Skill 文本来源（裁决后的 entries + L1 预算）。
        # 为什么单独一个 trace step：`omitted` 必须同时出现在指令与 trace 里，
        # 缺任何一边都是「静默丢内容」。这里同时收回这两个数给调用方。
        async with tracer.step(PREPARE, "skills.inject") as inject_step:
            skills_render = render_skill_instruction(resolved_skills, request.workspace_id)
            instruction = append_uploads(skills_render.text, uploads)
            inject_step.metadata["injected"] = len(skills_render.injected)
            inject_step.metadata["budgetChars"] = skills_render.budget_chars
            if skills_render.omitted:
                inject_step.metadata["omitted"] = len(skills_render.omitted)
                # 名字一起记：只有数字时，"哪些被截了"要靠人重新算预算才知道
                inject_step.metadata["omittedNames"] = ",".join(skills_render.omitted)

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
    catalog: ResolvedCatalog, uploads: list[str], workspace_id: str = ""
) -> str:
    """把 Skill 解析结果与上传文件路径拼成一次运行的 Instruction。"""
    return append_uploads(
        render_skill_instruction(catalog, workspace_id).text, uploads
    )


def append_uploads(instruction: str, uploads: list[str]) -> str:
    """把本次上传文件清单接在 Skill 段落之后（无附件时原样返回）。"""
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


@dataclass(frozen=True)
class SkillInstruction:
    """一次注入的成品与它的账目。

    为什么要带账目而不是只返回字符串：`omitted` 必须同时出现在**指令文本**与
    **trace** 里（设计稿 10.1「两者缺一即视为静默丢内容」）。只返回字符串的话，
    调用方要么自己再算一遍预算（两处实现必然漂），要么就漏掉 trace 那一半。
    """

    text: str = ""
    #: 真正进了 prompt 的条目，形如 `platform:current-time`
    injected: list[str] = field(default_factory=list)
    #: 被 L1 预算截掉的条目（同上格式），顺序 = 截断顺序
    omitted: list[str] = field(default_factory=list)
    budget_chars: int = 0


#: 作用域在注入文本里的展示顺序 = 优先级降序（与 resolver 的 entries 顺序一致）
_INJECT_LABELS = {
    SCOPE_PLATFORM: "platform",
    SCOPE_PROJECT: "project",
    SCOPE_USER: "user",
}

#: 为什么必须显式区分命名空间：平台 Skill 与项目文件都叫 `skills`，但前者在
#: 沙箱根下（`skills/custom`、`skills/project/<ws>`），后者是用户项目自己的
#: `workspace/<ws>/skills/`。只在提示里写「完整正文位于 …」而不说「新建写哪」，
#: 模型就会把「在这个项目里建个 skill」理解成写 `workspace/<ws>/skills/` ——
#: 落点错了但没有任何报错，表现为「创建成功、实际没有」。
_PLACEHOLDER_WORKSPACE = "<当前工作区>"


def _skill_path_guide(workspace_id: str = "") -> str:
    """Skill 存储位置说明（读 + 写）。

    **有工作区时填真实 ID**：提示里的 `<workspace-id>` 占位符会被模型原样抄进
    路径，`workspace_path` 校验第二段必须等于当前会话工作区 → `PermissionError`
    → 模型往往「换个能写的路径重试」，最终落回 `skills/custom`。表现就是
    「明确要求项目级，结果变成用户级」。填真值可以从源头消掉这条分支。

    没有工作区上下文时保留占位符：那时项目级本来就不可写，写死一个不存在的 ID
    只会更误导。
    """
    project_root = (
        f"{PROJECT_PATH}/{workspace_id}" if workspace_id else f"{PROJECT_PATH}/{_PLACEHOLDER_WORKSPACE}"
    )
    return (
        "## EDITH Skill 存储位置（与项目目录无关）\n"
        "以下目录属于 EDITH 平台，不是用户项目的一部分。它们位于 Sandbox 根目录 /home/user 下，"
        "在文件工具里用**相对路径**操作：\n"
        f"- 平台 Skills（只读）：{SYSTEM_PATH}/<目录>/SKILL.md\n"
        f"- 项目级 Skills（属于当前会话工作区）：{project_root}/<目录>/SKILL.md\n"
        f"- 用户级 Skills（跨工作区共享）：{CUSTOM_PATH}/<目录>/SKILL.md\n"
        "\n"
        "创建或修改 EDITH Skill 时：\n"
        "- 先判断作用域。用户级写 "
        f"{CUSTOM_PATH}/<skill-name>/；项目级写 {project_root}/<skill-name>/。\n"
        "- 「在这个项目里」「给这个仓库加个 skill」**不等于**项目级 Skill："
        "只有用户明确要求「项目级 Skill」或限定只在当前工作区生效时才用 project 层，否则一律用户级。\n"
        f"- 绝不把 EDITH Skill 写进 workspace/{workspace_id or _PLACEHOLDER_WORKSPACE}/skills/："
        "那是用户项目自己的 skills 目录，"
        "与平台 Skill 存储是两个命名空间，写错不会报错但也永远不会被加载。\n"
        "- 创建 EDITH Skill 时不要顺带改项目的 registry、package.json、CI 或文档，"
        "除非用户明确要求做项目改动。\n"
        "- 写完必须用文件工具读回目标 SKILL.md，确认文件真实存在且内容正确，再向用户报告创建成功。"
    )


def render_skill_instruction(catalog: ResolvedCatalog, workspace_id: str = "") -> SkillInstruction:
    """把解析后的 Skill 渲染成 Instruction 段落（B2-3 的新格式）。

    与旧格式（两段小标题 + 用户索引原文）的三个差别，都是一次性切换的结果：

    1. **唯一来源是 `entries`**（已裁决、name 唯一）。旧格式的「可用用户 Skills」
       直接铺索引原文，于是被项目级压住的那条也列在里面——模型看到两条同名规则，
       谁生效只能靠自己猜；
    2. **每条带作用域标签**。旧格式靠"在哪一段"表达优先级，段落消失后必须显式标注，
       否则去掉小标题就等于把优先级信息一起删了；
    3. **受 L1 预算约束**，被截掉的条目在文本里明写数量（不写名字：名字本身也占预算，
       而且 200 个名字的清单比省略更糟）。
    """
    entries = catalog.entries
    if not entries:
        return SkillInstruction(budget_chars=index_budget_chars())

    costs: list[tuple[str, str, int]] = []
    rendered: dict[int, str] = {}
    for entry in entries:
        line = _entry_line(entry)
        rendered[id(entry)] = line
        costs.append((entry.scope, entry.name, len(line)))

    plan = plan_index(costs)
    kept = set(plan.kept)

    lines = ["可用 Skills（按作用域从高到低排列，同名时靠前者生效）："]
    injected: list[str] = []
    for index, entry in enumerate(entries):
        if index not in kept:
            continue
        lines.append(rendered[id(entry)])
        injected.append(f"{entry.scope}:{entry.name}")

    if plan.omitted:
        # 预算与条数都写出来：只说"有省略"看不出是"差 1 个"还是"差 200 个"
        lines.append(
            f"（索引预算 {index_budget_chars()} 字符已满，另有 {plan.omitted_count} 个 Skill "
            "未列出。需要时请直接读对应目录，或让用户精简 Skill 数量。）"
        )

    # 路径说明是**固定结构**，不随 present 收窄：只列"当前出现过的作用域"时，
    # 用户级没有 Skill 就看不到 skills/custom 在哪 —— 而这恰恰是最需要写清楚的
    # 时刻（用户第一次要求建 Skill）。有工作区时带上真实 ID，避免模型照抄占位符。
    lines.append("")
    lines.append(_skill_path_guide(workspace_id))
    # 把 L2 额度提前说出来：不写的话模型会一路读到被硬拒（每次都是一轮工具调用
    # + 一条错误结果），而它本来可以直接依据摘要行事。这是"说清边界"，不是"靠自觉"
    # ——真正的拦截在 sandbox_read_file 里，这句只是省掉无谓的撞墙。
    lines.append(
        f"同一轮内最多读取 {MAX_SKILLS_PER_RUN} 个 Skill 的完整正文"
        f"（累计 {MAX_SKILL_BYTES_PER_RUN // 1024}KB）；超出会被拒绝，"
        "优先依据上面的摘要行动。"
    )
    return SkillInstruction(
        text="\n".join(lines),
        injected=injected,
        omitted=[f"{scope}:{name}" for scope, name in plan.omitted],
        budget_chars=index_budget_chars(),
    )


def _entry_line(entry) -> str:
    """一条 Skill 的注入行。

    只有**目录名与 name 不一致**时才补「目录」——`sandbox_path` 是用目录名拼的，
    而模型默认会按 name 找目录；两者不同时不写出来，模型就会去读一个不存在的路径，
    失败形态是「读到空 Skill」而不是报错。一致时不写，省下的都是 L1 预算。
    """
    label = _INJECT_LABELS.get(entry.scope, entry.scope)
    line = f"- [{label}] {entry.name}：{entry.description}"
    source_dir = entry.source_dir or entry.name
    if source_dir != entry.name:
        line += f"（目录：{source_dir}）"
    return line


def skill_instruction(catalog: ResolvedCatalog, workspace_id: str = "") -> str:
    """兼容包装：只要文本的调用方（测试、审计）用它。"""
    return render_skill_instruction(catalog, workspace_id).text
