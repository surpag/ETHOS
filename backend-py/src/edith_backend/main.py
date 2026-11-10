"""EDITH backend-v2 的 Python 重写版进程入口。

对应 Go 版 cmd/server/main.go 的组合根职责：
打开数据库、创建顶层模块、显式连接依赖、注册路由、启动服务。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

from . import config
from .database import open_database
from .internal import http_utils
from .internal.agentrun import Dependencies, Service
from .internal.agentrun.plan_tools import PlanToolSet
from .internal.agentrun.run_plan import RunPlanRegistry
from .internal.conversation import new_module as new_conversation_module
from .internal.cronadapter import Adapter as CronAdapter
from .internal.cronjob import create_schema as create_cronjob_schema
from .internal.cronjob import new_module as new_cronjob_module
from .internal.gateway import new_service as new_gateway_service
from .internal.gitops import resolve_git_identity
from .internal.gitops.http import HTTP as GitopsHTTP
from .internal.gitops.orchestrator import WorkspacePush
from .internal.images import new_module as new_images_module
from .internal.images.cos import COSConfig
from .internal.models import DEFAULT_MODEL_ID, CustomModels
from .internal.models import create_schema as create_models_schema
from .internal.models import new_module as new_models_module
from .internal.sandbox import HTTP as SandboxHTTP
from .internal.sandbox import AgentInput as SandboxAgentInput
from .internal.sandbox import SandboxToolSet
from .internal.sandbox import Service as SandboxService
from .internal.sandbox import create_schema as create_sandbox_schema
from .internal.session import SessionStore
from .internal.session import create_schema as create_session_schema
from .internal.session.constraint_store import ConstraintStore
from .internal.session.plan_store import RunPlanStore
from .internal.skills import HTTP as SkillsHTTP
from .internal.skills import Catalog as SkillsCatalog
from .internal.skills.storage import (
    SkillScopeStores,
    SkillStorageService,
    new_skill_backend,
    workspace_access_checker,
)
from .internal.systemtools import SystemToolSet
from .internal.tools import ToolRegistry
from .internal.trace import Store as TraceStore
from .internal.trace import create_schema as create_trace_schema
from .internal.usage import new_module as new_usage_module
from .internal.userconfig import new_module as new_userconfig_module
from .internal.userconfig.mcp_http import build_mcp_router
from .internal.userconfig.mcp_runtime import MCPRuntime
from .internal.volume import Service as VolumeService
from .internal.volume import Store as VolumeStore
from .internal.volume import create_schema as create_volume_schema
from .internal.webadapter import Adapter
from .internal.workspace import (
    WorkspaceMemoryStore,
    WorkspaceStore,
    WorkspaceVolumeService,
    build_instruction_router,
    build_memory_router,
    build_workspace_router,
)
from .internal.workspace import (
    create_schema as create_workspace_schema,
)
from .internal.workspace import (
    new_backend as new_workspace_volume_backend,
)

# 日志基础配置：模块级一次配好，所有 logger 统一格式并携带 request_id
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)

# 定义一个 ContextVar，用于在整个请求生命周期中传递 request_id
_request_id_var: ContextVar[str] = ContextVar("request_id", default="no-request")


# 自定义日志过滤器，自动给所有日志加上 request_id
class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id_var.get("no-request")
        return True


# 关键：filter 必须挂在 root 的 handler 上，而不是 root logger 上。
# logging 的 logger 级 filter 只对「发出日志的 logger 自身」生效；
# 子模块 logger 的 record 传播到 root 后由 handler 处理，handler 级 filter
# 才对所有经过的 record 生效（含第三方库日志）。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIDFilter())

# 日志落盘（可选）：设置 EDITH_LOG_FILE 后，同一格式的日志滚动写入文件
# （10MB × 5 个备份）。「跨 run 视角」（错误率 / 截断率 / P95 统计）依赖落盘——
# stdout 随终端滚动、重启即失。未设置时维持纯 stdout（dev 行为不变）。
_log_file = os.environ.get("EDITH_LOG_FILE")
if _log_file:
    from logging.handlers import RotatingFileHandler

    # handler 不建父目录，路径不存在会启动即崩——先建好再挂
    _log_dir = os.path.dirname(os.path.abspath(_log_file))
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _file_handler.addFilter(RequestIDFilter())
    logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时组装模块，退出时关闭资源。"""
    config.load_environment()
    db = await open_database(config.database_path())
    await create_session_schema(db)
    await create_volume_schema(db)
    await create_workspace_schema(db)
    await create_sandbox_schema(db)
    await create_cronjob_schema(db)
    await create_models_schema(db)
    await create_trace_schema(db)

    # trace 保留期：默认 14 天（失败/降级 run 内部翻倍）。多租户下不清理会无限增长。
    traces = TraceStore(db)
    try:
        removed = await traces.cleanup(int(os.getenv("EDITH_TRACE_RETENTION_DAYS", "14")))
        if removed:
            logger.info("trace_cleanup removed=%d", removed)
    except Exception:  # 清理失败不阻塞启动
        logger.warning("trace cleanup failed", exc_info=True)

    users = await new_userconfig_module(db, DEFAULT_MODEL_ID)
    custom_models = CustomModels(db, users.providers)
    model_catalog = new_models_module(users.providers, custom_models)
    usage = await new_usage_module(db)
    session_store = SessionStore(db)
    plan_store = RunPlanStore(db)
    # 活跃计划注册表：plan_update 工具写入、run 收尾读取、历史走 plan_store，
    # 三者共用一个实例，避免出现「工具改了、收尾看不到」的双份状态。
    run_plans = RunPlanRegistry()
    conversations = new_conversation_module(session_store, usage.reader, plan_store)

    volumes = VolumeService(VolumeStore(db))
    workspaces = WorkspaceStore(db)
    workspace_backend = new_workspace_volume_backend(
        os.getenv("EDITH_WORKSPACE_VOLUME_BACKEND", "e2b"), workspaces
    )
    workspace_volumes = WorkspaceVolumeService(workspaces, workspace_backend)
    workspace_memories = WorkspaceMemoryStore(db)

    # 用户 Skill 存储：默认本地目录（E2B Volumes 仍是 private beta，未开通会 403）。
    # 上层（catalog / sandbox / agentrun）只依赖门面，切换后端不需要改业务代码。
    skill_storage = SkillStorageService(
        new_skill_backend(os.getenv("EDITH_USER_SKILLS_BACKEND", "local"), volumes)
    )
    # 作用域分发点：项目级要绑定 workspace，且**必须**带归属判定器
    # （`workspace/store.exists` —— 只回 bool，不区分"不存在"与"不属于你"）。
    # 不传判定器时项目级 fail-closed：读返回 unavailable、写拒绝且不建目录。
    skill_scopes = SkillScopeStores(
        os.getenv("EDITH_USER_SKILLS_BACKEND", "local"),
        user_service=skill_storage,
        access_checker=workspace_access_checker(workspaces),
    )

    sandboxes = SandboxService(
        db,
        os.getenv("EDITH_E2B_TEMPLATE", "default"),
        volumes,
        workspaces,
        workspace_volumes,
        skill_scopes=skill_scopes,
    )
    async def _git_identity(user_id: str) -> tuple[str, str]:
        """解析用户的 git 提交身份：用户设置优先，留空回退平台默认。"""
        settings = await users.settings.load(user_id)
        return resolve_git_identity(settings.git_author_name, settings.git_author_email)

    # 推送编排只建一份，HTTP「推送到 GitHub」与 Agent 的 git_push 工具共用
    git_push = WorkspacePush(sandboxes, users.git_credentials, workspaces)
    sandbox_tools = SandboxToolSet(
        sandboxes, _git_identity, git_push.push, git_push.remote_info
    )
    sandbox_input = SandboxAgentInput(sandboxes)
    skills = SkillsCatalog(skill_storage)
    cron_jobs = await new_cronjob_module(db, users.settings)
    images = await new_images_module(
        db,
        COSConfig(
            bucket=os.getenv("EDITH_COS_BUCKET", ""),
            region=os.getenv("EDITH_COS_REGION", ""),
            secret_id=os.getenv("EDITH_COS_SECRET_ID", ""),
            secret_key=os.getenv("EDITH_COS_SECRET_KEY", ""),
        ),
    )

    tools = ToolRegistry(
        [SystemToolSet(), sandbox_tools, cron_jobs.tools, PlanToolSet(run_plans, plan_store)]
    )
    mcp_runtime = MCPRuntime(users.mcp)

    agent_runs = Service(
        Dependencies(
            models=model_catalog.catalog,
            settings=users.settings,
            providers=users.providers,
            store=session_store,
            usage=usage.recorder,
            skills=skills,
            skill_scopes=skill_scopes,
            images=images.agent_input,
            files=sandbox_input,
            mcp=mcp_runtime,
            memories=workspace_memories,
            constraints=ConstraintStore(db),
            plans=run_plans,
            plan_store=plan_store,
            tool_definitions=tools.tool_definitions(),
            tool_executor=tools,
            traces=traces,
            skill_flush=sandboxes.flush_skill_sync,
        )
    )
    gateway = new_gateway_service(users.bindings, agent_runs)
    web = Adapter(gateway)
    cron_adapter = CronAdapter(gateway)
    cron_scheduler = cron_jobs.new_scheduler(cron_adapter)

    app.include_router(users.http.router)
    app.include_router(build_mcp_router(users.mcp))
    app.include_router(model_catalog.http.router)
    app.include_router(build_workspace_router(workspaces, workspace_volumes))
    app.include_router(build_instruction_router(workspace_memories))
    app.include_router(build_memory_router(workspace_memories))
    app.include_router(conversations.http.router)
    app.include_router(SkillsHTTP(skills, skill_scopes).router)
    app.include_router(SandboxHTTP(sandboxes).router)
    app.include_router(GitopsHTTP(git_push).router)
    app.include_router(cron_jobs.http.router)
    app.include_router(images.http.router)
    app.include_router(web.router)

    app.state.db = db
    scheduler_task = asyncio.create_task(cron_scheduler.run())

    yield

    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    await db.close()


def create_app() -> FastAPI:
    """创建并组装 FastAPI 应用。"""
    app = FastAPI(title="EDITH backend", version="0.1.0", lifespan=lifespan)
    http_utils.register_error_handler(app)

    # 方案 C（2026-09-01）：允许浏览器直传大文件（绕过 Next.js BFF 流式转发不可靠问题）。
    # 生产环境应收紧为前端域名白名单。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def add_observability(request: Request, call_next):
        # 生成或提取 request_id
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # 绑定到 ContextVar（之后所有日志自动带上它）
        token = _request_id_var.set(request_id)

        # 记录请求开始（结构化日志）
        logger.info(
            "http_request_start method=%s path=%s client_ip=%s",
            request.method,
            request.url.path,
            request.client.host if request.client else None,
        )

        start_time = time.time()
        try:
            response = await call_next(request)

            # 记录请求结束（包含耗时）
            duration_ms = (time.time() - start_time) * 1000
            logger.info(
                "http_request_end status=%d duration_ms=%.2f",
                response.status_code,
                duration_ms,
            )

            # 把 request_id 原样返回给客户端（方便前端联调）
            response.headers["X-Request-ID"] = request_id
            return response

        except Exception:
            # 捕获未处理的异常（防止中间件把异常吞掉，但记录日志）
            duration_ms = (time.time() - start_time) * 1000
            logger.exception("http_request_exception duration_ms=%.2f", duration_ms)
            raise  # 交给 FastAPI 的全局异常处理器
        finally:
            # 重置 ContextVar，避免污染其他请求
            _request_id_var.reset(token)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    """开发启动入口：`uv run edith-backend`。"""
    import uvicorn

    config.load_environment()
    host, _, port = config.runtime_address().rpartition(":")
    uvicorn.run(
        "edith_backend.main:app",
        host=host or "127.0.0.1",
        port=int(port) if port else 8080,
        reload=True,
    )
