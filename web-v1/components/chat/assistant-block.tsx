import { BrainCircuit, ChevronDown, CircleAlert, Clock } from "lucide-react";
import { memo } from "react";

import type { AssistantBlock, CompletionEvidence } from "@/lib/chat/type";
import { MessageMarkdown } from "./message-markdown";
import { ToolCard } from "./tool-card";

// 耗时展示：不足 1 秒给毫秒，1 分钟内给一位小数的秒，更长给 分+秒
function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  const seconds = ms / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

// 完成证据：把「模型结束了」和「本轮真实发生过什么」分开呈现。
// 这一步的意义不在于好看——模型可以用一段文本声称完成了任何事，
// 而这里的每个字都来自事实（工具调用记录），所以它是用户唯一能核对的东西。
const EVIDENCE_META: Record<CompletionEvidence, { label: string; className: string; hint: string }> = {
  mutated: {
    label: "已产生变更",
    className: "text-success",
    hint: "本轮有写文件 / 执行命令等改动型工具成功执行",
  },
  external: {
    label: "外部工具",
    className: "text-muted",
    hint: "本轮调用了 MCP 工具，具体效果未核实",
  },
  metadata_only: {
    // 计划更新之类的元工具：它确实写了我们库里的状态，但对文件/仓库/外部系统
    // 没有任何影响。把它显示成「已产生变更」会让这个最高等级变得人人都有。
    label: "仅更新计划",
    className: "text-muted",
    hint: "本轮只更新了任务计划等元数据，没有改动文件或执行命令",
  },
  read_only: {
    label: "仅查询",
    className: "text-faint",
    hint: "本轮只读取了信息，没有修改任何内容",
  },
  none: {
    label: "未调用工具",
    className: "text-warning",
    hint: "本轮没有任何工具调用；如果回复里说「已完成 / 已创建」，请以文件与命令结果为准",
  },
};

// 终止原因 → 提示文案。原因只影响「说什么」，不影响「要不要提示」：
// 未知原因一律走兜底，不会因为后端新增原因就变成静默。
const INCOMPLETE_REASON_COPY: Record<string, string> = {
  rounds_exhausted: "已达到本轮工具调用上限，任务可能还没做完。发送「继续」可接着执行。",
  context_budget_exhausted: "上下文已占满，本轮提前收尾。发送「继续」会先压缩历史再接着执行。",
  output_truncated: "模型输出被长度上限截断，本轮没有正常收尾。发送「继续」可接着执行。",
  content_filtered: "输出被内容安全策略中断，本轮没有正常收尾。",
};

function incompleteCopy(reason?: string | null): string {
  if (typeof reason === "string" && INCOMPLETE_REASON_COPY[reason]) {
    return INCOMPLETE_REASON_COPY[reason];
  }
  return "本轮由外力中断，任务可能还没做完。发送「继续」可接着执行。";
}

export const AssistantBlockView = memo(function AssistantBlockView({ block }: { block: AssistantBlock }) {
  // null / undefined = 未知（旧消息）：不显示任何证据标记，避免凭空下结论
  const evidence = block.completionEvidence
    ? (EVIDENCE_META[block.completionEvidence] ?? null)
    : null;
  // 只认语义：incomplete 才提示。这里刻意不判断具体原因 ——
  // 后端每加一种终止原因，枚举式判断就会静默漏掉一种「没做完」。
  const incomplete = block.outcome === "incomplete";
  return (
    <article className="space-y-4 text-sm leading-7 text-ink">
      {block.blocks.map((content, index) => {
        const contentKey = content.id ?? `content-${index}`;
        if (content.type === "reasoning") {
          return (
            <details className="group rounded-xl border border-border bg-surface text-muted shadow-sm shadow-black/[0.02]" key={contentKey}>
              <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-xs font-medium select-none [&::-webkit-details-marker]:hidden">
                <span className="inline-flex size-6 items-center justify-center rounded-lg bg-accent-soft text-accent"><BrainCircuit className="size-3.5" /></span>
                <span className="text-ink">思考过程</span>
                <span className="truncate text-faint">模型推理内容</span>
                <ChevronDown className="ml-auto size-3.5 shrink-0 text-faint transition-transform group-open:rotate-180" />
              </summary>
              <p className="border-t border-border px-3 py-3 whitespace-pre-wrap text-xs leading-6 text-muted">{content.content}</p>
            </details>
          );
        }
        if (content.type === "tool") {
          return <ToolCard block={content} key={contentKey} />;
        }
        return <MessageMarkdown content={content.content} key={contentKey} />;
      })}
      {(typeof block.durationMs === "number" || evidence) && (
        <p className="flex items-center gap-1.5 text-[11px] text-faint">
          {typeof block.durationMs === "number" && (
            <>
              <Clock className="size-3" />
              用时 {formatDuration(block.durationMs)}
            </>
          )}
          {evidence && (
            <>
              {typeof block.durationMs === "number" && <span className="text-border-strong">·</span>}
              <span className={evidence.className} title={evidence.hint}>
                {evidence.label}
              </span>
            </>
          )}
        </p>
      )}
      {incomplete && (
        <div
          className="flex items-start gap-2 rounded-lg bg-warning-soft px-3 py-2 text-xs leading-5 text-warning"
          role="status"
        >
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
          <p>
            <span className="font-medium">任务未完成</span>
            <span className="mx-1.5 opacity-50">·</span>
            {incompleteCopy(block.endReason)}
          </p>
        </div>
      )}
    </article>
  );
});
