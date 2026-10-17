# 可观测性增强方案（方向 1）

> 状态：设计稿（未实施）
> 目标：任何一次请求出问题，能通过日志回答三件事——**发生了什么、在哪一环、用了多久**。
> 定位：多用户服务端 AI Agent 平台（单人维护），排错效率是最高优先级投入。

---

## 一、现状诊断

| 问题 | 证据 |
|------|------|
| 无全局日志配置 | `backend-py/src/edith_backend/main.py` 无任何 `logging` 配置；应用 logger 走 Python 默认（WARNING 级别、无格式），`logger.info()` 写了也不输出 |
| 核心链路无日志 | 全项目仅 3 个文件有 logger：`internal/cronjob/scheduler.py`、`internal/userconfig/mcp_runtime.py`、`internal/volume/service.py`。AgentRun 执行链路（`internal/agentrun/service.py`）、HTTP 入口（`internal/webadapter/adapter.py`）、用量、模型、沙箱均无日志 |
| 业务错误不落日志 | `internal/http_utils.py` 的 `register_error_handler` 捕获 `EDITHError` 后只返回 JSON，不打印任何日志——后端抛业务错误，日志里无据可查 |
| MCP 连接成功不可见 | `mcp_runtime.py` 只有失败 `warning`，成功无日志 |

## 二、设计原则与红线

- **最小改动**：零架构改动，只加日志。
- **结构化**：统一 `key=value` 格式，业务日志带 `request_id`。
- **红线 1**：绝不打印 API Key / token / 用户密钥（`providers.py`、MCP headers 的值一律脱敏）。
- **红线 2**：不引入 APM / 链路追踪 / Prometheus / ELK——单机部署，日志足够，堆这些是过度设计。

## 三、方案（4 块）

### 块 A：日志基础设施

**位置**：`main.py` 的 `lifespan` 或模块级

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
```

**效果**：`logger.info()` 生效、格式统一。这是当前「日志写了不输出」的根因。

### 块 B：HTTP 请求日志（中间件）

**位置**：`main.py` 的 `create_app()` 加 `@app.middleware("http")`

**记录**：每个请求的 `method path status duration_ms client_ip`；4xx/5xx 用 WARN 级别。

```
INFO  http  POST /internal/gateway/messages:stream status=200 duration_ms=2140 ip=127.0.0.1
WARN  http  GET  /internal/sandbox/files status=404 duration_ms=8 ip=127.0.0.1
```

**收益**：HTTP 层问题（404/500/超时/慢接口）一眼可见，覆盖全部 BFF 转发入口。

### 块 C：AgentRun 生命周期日志（核心链路）

**位置**：`internal/agentrun/service.py` 的 `run()` 与 `_run()`

- `run()` 入口：`agentrun start request_id=... user=... session=... model=...`
- `_run()` 完成：`agentrun end request_id=... duration_ms=... tokens=... tools=... mcp_tools=... reason=completed|canceled|error`
- `_run()` 异常分支：`agentrun error request_id=... error=...`

**关键点**：`request_id` 由前端传入（`Request.request_id`），日志直接用它，天然串联「一次对话的完整生命周期」。`reason` 与现有事件类型（`run.completed` / `run.canceled` / `run.error`）对齐。

### 块 D：补缺漏的两处日志

1. **`internal/userconfig/mcp_runtime.py`**：`_connect_server` 成功时补
   `logger.info("mcp connected server=%s transport=%s tools=%d", ...)`；失败 warning 保留。
2. **`internal/http_utils.py`** 的 error handler：捕获 `EDITHError` 时补
   `logger.warning("http error status=%s type=%s path=%s", ...)`，业务错误有据可查。

## 四、落地清单（共 4 个文件）

| 文件 | 改动 |
|------|------|
| `src/edith_backend/main.py` | + logging 配置 + HTTP 中间件 |
| `src/edith_backend/internal/agentrun/service.py` | + start / end / error 三处日志 |
| `src/edith_backend/internal/userconfig/mcp_runtime.py` | + 连接成功日志 |
| `src/edith_backend/internal/http_utils.py` | + error handler 补日志 |

## 五、验证方式

1. 发一条消息 → 日志出现 `agentrun start` + `agentrun end`（含耗时/token）；
2. 调一个不存在的接口 → `http` 日志出现 404；
3. 断开后端再请求 → `http` 日志出现 502，且 `agentrun` 异常链可查；
4. 连接 GitHub MCP → 出现 `mcp connected server=github tools=N`。

## 六、后续可扩展（暂不做，克制）

- 日志文件滚动输出（`RotatingFileHandler`），需要保留历史时再加；
- 敏感字段自动脱敏的通用工具函数（当前靠约定，手工保证不打印）。
