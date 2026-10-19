# EDITH backend (Python)

EDITH 服务端 Agent 平台的 **Python 重写版**，1:1 对齐 Go 版 [`backend-v2/`](../backend-v2/) 的功能与对外契约。

> 动机：学习 Python 生态。Agent 内核采用「OpenAI SDK + 手写编排循环」的轻量方案（方案 B），
> 继承 Go 版「少抽象、显式线性、模块自治」的架构哲学，不使用 LangChain/LangGraph 全家桶。
> 前端（`web-v1/`）**完全不变**，只要对外 HTTP/SSE 契约保真即可无缝切换。

## 架构

与 Go 版保持同一套纵向模块自治结构，主流程线性：

```
Web浏览器 / 定时任务 / 未来IM渠道
              │
              ▼
          Adapter          ← 协议翻译（HTTP SSE / Cron）
              │
              ▼
          Gateway          ← 身份认证 + 请求边界
              │
              ▼
          AgentRun         ← 聚合本次运行配置
              │
              ▼
        OpenAI SDK 流式循环  ← 手写编排（方案 B）
              │
              ▼
         中性 Event         ← 渠道无关的流式事件
```

## 目录结构

```
src/edith_backend/
├── main.py                 # 组合根 + FastAPI 应用
├── config.py               # 环境变量加载
├── database.py             # SQLite 连接 + WAL
└── internal/
    ├── agentrun/           # 核心编排：准入 → 配置聚合 → 流式执行
    ├── agentstream/        # 中性事件模型与流式解码
    ├── gateway/            # 渠道统一入口
    ├── webadapter/         # HTTP/SSE 适配器
    ├── conversation/       # Timeline 投影
    ├── session/            # 消息、任务状态、检查点
    ├── models/             # 内置 + 用户自定义模型目录
    ├── userconfig/         # 设置、供应商密钥、MCP、渠道绑定
    ├── sandbox/            # E2B 沙箱生命周期与工具集
    ├── workspace/          # 工作区实体、存储后端与双向同步
    ├── volume/             # 持久化卷
    ├── skills/             # Skill 目录
    ├── images/             # COS 图片
    ├── gitops/             # Git 推送与凭据注入
    ├── cronjob/            # 定时任务存储与调度
    ├── cronadapter/        # 定时执行适配
    ├── usage/              # Token 用量
    ├── trace/              # 链路追踪（含 CLI viewer）
    ├── systemtools/        # 系统工具
    ├── tools.py            # 工具注册中心
    └── netguard.py         # 共享 SSRF / 出口校验
```

## 技术栈

- 后端：Python 3.13、FastAPI、uvicorn、Pydantic v2、openai SDK
- 存储：SQLite（aiosqlite）
- 调度：自实现轮询调度器（对齐 Go 版，10 秒轮询 + SQL 原子抢占）
- 身份：Clerk（后端仅读 BFF 注入的 userId）
- Agent 工作区：E2B Sandbox + Volume
- 图片存储：腾讯云 COS

## 本地启动

依赖已安装到 `.venv/`（uv 在此环境有 cache 锁问题，用 venv 直启更稳）：

```powershell
cd backend-py

# 方式一：直接用 venv 启动（推荐）
.venv\Scripts\python.exe -m uvicorn edith_backend.main:app --reload --host 127.0.0.1 --port 8080

# 方式二：uv 启动（依赖 uv 正常时）
uv sync
uv run edith-backend
```

启动后访问：`http://127.0.0.1:8080/health`

环境变量在项目根目录 `.env`（E2B、COS 配置），默认从 `../.env` 加载。

## 运行测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## 开发进度

- [x] 阶段 0：脚手架（FastAPI + /health）
- [x] 阶段 1：配置 + 数据库 + 模型目录
- [x] 阶段 2：agentstream + agentrun 核心
- [x] 阶段 3：gateway + webadapter + SSE
- [x] 阶段 4：conversation + usage
- [x] 阶段 5：sandbox + volume + skills
- [x] 阶段 6：MCP + cronjob
- [x] 阶段 7：images + systemtools
- [x] 阶段 8：前端联调 + 回归（冒烟测试通过）
- [x] 阶段 9：MCP streamable HTTP / SSE 双传输
- [x] 阶段 10：工作区绑定、自定义模型、Git 推送、深层安全加固

> 当前测试规模：`tests/` 下 220+ 用例，覆盖迁移、同步删除传播、用量核算、Git 推送与 MCP。
