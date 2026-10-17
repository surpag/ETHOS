# trace 模块

一次 run 的分步耗时 / 异常 / 降级记录。对应 `docs/trace-eval-design.md` 的 P0 + P1。

> 注意与 `internal/volume/trace.py` 区分：后者是 E2B HTTP 调试开关，与本模块无关。

## 回答什么问题

| 现场 | 怎么查 |
|---|---|
| 「一直转圈」 | `list --running` 看有无未收尾的 run；日志里找不成对的 `llm_call_start` |
| 「报错了」 | `show <request_id>` 末尾直接打印 Traceback（补上了此前的异常零日志缺口） |
| 「怎么这么慢」 | `show` 的耗时分解：墙钟 / 自耗时 / **未打点**；`slow` 看跨 run 聚合 |
| 「LLM 一直在思考」 | `llm_call` 步的 `ttft_ms`（到首个增量）与 `first_content_ms`（到首段正文）、`reasoning_chars` |
| 「token 怎么这么多」 | `llm_call` 步的 `tokens` + `msgs`，`tool_call` 步的 `cached` |
| 「今天全挂」 | `slow` 的按环节聚合（次数 / 平均 / 最慢 / 失败数） |

## CLI

```bash
python -m edith_backend.internal.trace.viewer list --user u_xxx --limit 20
python -m edith_backend.internal.trace.viewer list --running          # 只看未收尾
python -m edith_backend.internal.trace.viewer show <request_id>       # 步骤树 + Traceback
python -m edith_backend.internal.trace.viewer slow --days 7 --min-ms 5000
```

也可以直接用控制台脚本 `edith-trace`（`uv run edith-trace ...`）。只读，不需要后端在跑；
默认读 `EDITH_DB_PATH`，可用 `--db` 覆盖。

## 打点

```python
async with tracer.step(LLM_CALL, "create#1", input={"messages": 12}) as s:
    ...
    s.output = "首段文本"            # 退出时压成摘要
    s.metadata["ttft_ms"] = 430      # 结构化字段，可聚合
```

`step()` 自动做三件事：计时、父子关联（ContextVar）、异常捕获（记录后继续抛）。

已埋点：`prepare`（7 个子阶段）、`mcp`、`resume`、`summary`、`llm_call`、`tool_call`、`memory`。

## 关键取舍

- **落盘介质 = SQLite 两表**（`traces` + `trace_steps`）：后端可多副本，本地文件查不到；
  且 JSON 必须结束时一次写，进程被杀就全丢。
- **按轮 flush**，不逐条 commit：本库是 DELETE journal 模式，逐条 commit 会在工具路径上
  产生双倍 fsync；而恢复职责由 `run_checkpoints` 承担，trace 只做诊断。
- **原地语义**：`trace_steps` 只写**已完成**的步骤。因此「挂住的那一步」不在表里——
  定位它靠 `traces.status = running`（`list --running`）+ 日志里不成对的 `llm_call_start`。
- **默认只存摘要**（`len` + `sha256[:12]`），`EDITH_TRACE_FULL=1` 时存原文截断。
  多租户下 trace 会碰到用户代码内容，默认不落原文。

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `EDITH_TRACE_SLOW_MS` | 10000 | 单步超此阈值即告警 |
| `EDITH_TRACE_FULL` | 关 | 记录 input/output 原文（排错临时开） |
| `EDITH_TRACE_RETENTION_DAYS` | 14 | 保留期；失败/降级 run 内部翻倍 |

## 已知未打点

- 消息/checkpoint 落库（`persist`）：等 `show` 里的「未打点」数字大到值得埋了再加。
- 自动记忆里的第二轮模型调用已包在 `memory` 步里，但粒度为整体耗时。
