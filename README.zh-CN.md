# EDITH

[English](README.md) · 一个面向多用户的可扩展服务端 AI Agent 平台。

EDITH 不只是一个聊天页面，而是一个可长期演进的**多用户服务端 Agent 平台**：每位用户拥有独立身份、配置、会话、沙箱与工作区；用户可与模型流式对话，让 Agent 读写真实工程文件、沉淀可复用的 Skill、接入 MCP 服务、把工作区推送到 Git 仓库，并按 Cron 定时自动执行任务。

> 后端位于 [`backend-py/`](backend-py/)（Python 3.13 + FastAPI）；`web-v1/` 使用的 HTTP/SSE 契约保持不变。更早的 Go 实现在 [`backend-v2/`](backend-v2/) 中保留，供参考与对比。

在线体验：<https://surpag.de5.net/>

> 当前以 Web 为第一个用户渠道；飞书、Telegram、GitHub App 等 IM 渠道留待后续接入——Gateway 这一层边界已经为它们留好。

## 项目特点

- **真正的 Agent 运行时，而不是对模型的封装**：所有渠道的请求先经唯一 `Gateway` 归一化为一次请求，`AgentRun` 负责聚合本次运行所需的模型、密钥、人格、Skills、工作区记忆、图片与工具，再由**手写的 Function Calling 循环**驱动流式接收、工具分发与结果回传。中间不夹任何 Agent 框架——每个 token、每次工具调用、每个取消点都在自己手里。
- **多用户是结构性的，不是约定**：Clerk 身份贯穿请求、会话、配置、沙箱、卷与定时任务，用户之间不共享运行空间和凭据；工具校验的是**授权**（本会话能否访问这个工作区），而不只是路径格式。
- **工作区只有一个权威副本**：工作区绑定真实项目目录，存储后端收敛为一个实现（`e2b` 持久化卷或 `local` 本地目录），Agent 始终读写权威副本，不再与镜像副本互相覆盖。
- **删掉的就是真的删掉了**：以持久化的「两侧共识文件集合」驱动双向同步，一次集合差收敛所有删除来源，一侧删除不会被另一侧重建复活。
- **故障是降级，不是中断**：沙箱故障转为模型可感知的工具错误，模型调用沿「请求模型 → 用户默认 → 平台默认」回退，图片读取失败降级为纯文本输入；所有降级在终态事件中显式标记。
- **成本可计量**：每次 run 的真实流式用量（含缓存 token）都被采集，严格区分「命中 0」与「未知」，汇总附带覆盖率。
- **Skills 渐进加载**：只把摘要放进上下文，正文按需读取，知识库可以持续扩张而不侵占 token 预算。
- **全链路可追踪**：`request_id` 从 HTTP 边界贯穿到每次工具调用，分步耗时以 JSONL 落盘，配套 CLI（`edith-trace`）支持列出、查看与慢请求排序。

## 实拍图

![对话工作台](docs/assets/screenshots/chat-workspace.png)

![扩展中心](docs/assets/screenshots/extensions.png)

![定时任务](docs/assets/screenshots/scheduled-tasks.png)

![Sandbox 文件区](docs/assets/screenshots/sandbox-files.png)

![设置](docs/assets/screenshots/settings.png)

## 已实现功能

### Agent 对话

- 流式回复、思考过程与工具调用卡片
- 多供应商模型：内置目录为只读权威源，用户可在界面自助添加供应商与自定义模型，无需改代码或重新部署
- 图片输入，存储于腾讯云 COS，历史消息可回填
- 会话接近当前模型上下文窗口 40% 时生成滚动摘要；摘要请求自身溢出时裁剪最旧消息重试，失败则回退全量历史
- 基于 `request_id` 的运行状态查询与中断；客户端断连即取消任务并释放资源，不留悬挂请求
- 中断后可续跑：任务状态 + 工具调用检查点，让下一次运行接着做而不是从头开始

### 工作区与沙箱

- 每个 `user_id + session_id` 拥有独立 E2B 沙箱，提供文件、目录与进程工具
- 工作区可绑定项目目录，支持工作区指令、记忆与出口域名白名单
- 存储后端可切换；双向同步具备免下载比对、单文件体积上限、二进制排除、失败熔断与显式超时
- 聊天页内查看文件树；原始资料上传至 `/uploads`，交付物从 `/artifacts` 下载
- 出口白名单由沙箱网络策略强制执行

### Skills、MCP 与 Git

- 公共 Skills 随服务内置并挂载进每个沙箱；用户 Skills 保存在自己的持久化卷中
- `overview.md` 同时为 Agent 上下文和扩展页提供稳定、低成本的 Skill 摘要
- 远程 MCP 服务支持 **streamable HTTP** 与 **SSE** 两种传输，可在界面配置与启停；连接生命周期绑定开启它的那次运行
- 工作区可推送到 Git 远程：沙箱只负责生成 bundle，后端以环境变量注入凭据完成推送——凭据不进沙箱、不落盘、不进命令行

### 定时任务

- 一次性任务与周期性 Cron 任务，支持用户时区与默认模型
- SQL 原子抢占，并发与多实例下任务不会重复执行
- 每次执行都经过同一个 Gateway，并在独立会话中保存结果

## 架构一眼看懂

渠道、执行与基础设施彼此分离，主链路刻意保持短：

```text
浏览器 ──► Next.js BFF ──► WebAdapter ┐
                                      ├──► Gateway ──► AgentRun ──► Function Calling 循环
定时调度器 ──► CronAdapter ───────────┘    身份边界      配置聚合          │
                                                                         ▼
                                                          中性 Agent 事件（SSE）
```

后端按显式模块组织。每个模块管理自己的存储、自己的 HTTP 边界（若需要），并暴露自己的公开能力；`main.py` 只负责创建模块与连接依赖。

```text
backend-py/src/edith_backend/
├─ main.py            组合根、中间件与路由装配
├─ config.py          环境变量加载
├─ database.py        SQLite 连接与 WAL
└─ internal/
   ├─ agentrun/       准入 → 配置聚合 → 流式执行
   ├─ agentstream/    渠道中性事件模型与流式解码
   ├─ gateway/        所有渠道共用的身份与请求边界
   ├─ webadapter/     Web BFF 的 HTTP / SSE 适配
   ├─ cronadapter/    定时执行 → Gateway 消息
   ├─ conversation/   会话历史投影为浏览器时间线
   ├─ session/        消息、任务状态、检查点与用量记录
   ├─ models/         内置模型目录 + 用户自定义模型
   ├─ userconfig/     设置、供应商密钥、MCP 与渠道绑定
   ├─ sandbox/        E2B 生命周期、文件/命令工具与文件 HTTP
   ├─ workspace/      工作区实体、存储后端与双向同步
   ├─ volume/         用户级持久化卷
   ├─ skills/         公共与用户 Skill 目录
   ├─ images/         图片元数据与 COS 访问
   ├─ gitops/         受护栏约束的 Git 执行与凭据注入
   ├─ cronjob/        任务存储、调度器与 Agent 工具
   ├─ usage/          Token 计量与汇总
   ├─ trace/          运行分步耗时、JSONL 存储与 CLI 查看器
   ├─ systemtools/    不依赖用户数据的系统工具
   ├─ tools.py        工具注册中心
   └─ netguard.py     共享的 SSRF / 出口校验
```

有两条规则支撑整个设计：

- **一种能力只有一个权威实现** —— 存储后端、模型目录、推送入口各自收敛为一个实现类，而不是散落在多个文件里的条件分支。
- **诊断信息不参与控制流** —— 终止原因与 trace 只进日志和界面；是否续跑只看任务状态与检查点。

## 可靠性与安全

| 关注点 | 做法 |
| --- | --- |
| 沙箱不可用 | 沙箱异常转为工具结果，Agent 自动转纯文本回答，而非让整轮失败 |
| 模型不可用 | 可重试错误有限重试，随后沿请求模型 → 用户默认 → 平台默认回退，并在终态事件中标记 |
| 删除 | 共识集合传播 + 五道护栏（无基线、沙箱重建、单轮上限、整体消失、独立开关） |
| 路径越权 | 单一授权卡点校验所有带路径的工具，含 move 这类多路径字段的工具 |
| SSRF | 私网与回环地址拒绝；云元数据端点无条件拒绝 |
| Git 凭据 | 后端以环境变量注入，不落盘、不进 argv、不进沙箱 |
| 供应商密钥 | 带版本前缀的混淆存储，并明确其「混淆而非加密」的边界 |

## 技术栈

- **后端**：Python 3.13、FastAPI、uvicorn、asyncio、Pydantic v2、OpenAI SDK（编排循环手写）
- **存储**：SQLite（`aiosqlite`，WAL、在线备份、迁移）
- **沙箱**：E2B 沙箱与持久化卷；图片存储使用腾讯云 COS
- **前端**：Next.js 16、React、TypeScript、Tailwind CSS
- **身份认证**：Clerk
- **部署**：Docker Compose、Nginx（针对 SSE 调优）、Cloudflare Tunnel
- **质量**：`pytest`（220+ 用例，覆盖迁移、同步删除传播、用量核算等）、`ruff`

## 本地启动

1. 复制环境变量模板，填入 Clerk、模型供应商、E2B 与 COS 的配置：

   ```bash
   cp .env.example .env
   ```

2. 启动后端：

   ```bash
   cd backend-py
   uv sync
   uv run uvicorn edith_backend.main:app --reload --host 127.0.0.1 --port 8080
   ```

   不使用 `uv` 时：

   ```bash
   cd backend-py
   python -m venv .venv
   ./.venv/Scripts/python.exe -m pip install -e .
   ./.venv/Scripts/python.exe -m uvicorn edith_backend.main:app --reload --host 127.0.0.1 --port 8080
   ```

3. 在另一个终端启动 Web：

   ```bash
   cd web-v1
   npm install
   npm run dev
   ```

4. 打开 `http://localhost:3000`。

或用 Docker 一键启动整套（读取根目录 `.env`）：

```bash
docker compose up -d --build
```

## 目录说明

| 路径 | 作用 |
| --- | --- |
| `backend-py/` | 当前后端 —— Python 3.13 + FastAPI |
| `web-v1/` | Next.js Web 客户端与 BFF |
| `deploy/` | 部署、TLS、隧道与备份运维手册 |
| `e2b-template/` | E2B 沙箱模板定义 |
| `docs/` | 架构、协议、设计与复盘文档 |
| `backend-v2/` | 更早的 Go 实现，保留供参考 |
| `edith-studio/` | 本地桌面 Coding Agent 客户端 |

## 文档

- [`docs/project-walkthrough.md`](docs/project-walkthrough.md) —— 全系统端到端导读
- [`docs/cloud-multitenant-migration.md`](docs/cloud-multitenant-migration.md) —— 多租户迁移与工作区存储模型
- [`docs/observability.md`](docs/observability.md) —— trace 结构与排障方式
- [`deploy/README.md`](deploy/README.md) —— 部署、HTTPS、隧道、备份与恢复

## 当前状态

平台核心已完成并在生产运行：对话、工具循环、沙箱、工作区与同步、Skills、MCP、Git 推送、定时任务、用户配置与上下文压缩协同工作。新增渠道只需按 Gateway 契约写一个适配器，执行内核不需要改动。
