import { BrainCircuit, ChevronDown, Clock } from "lucide-react";
import { memo } from "react";

import type { AssistantBlock } from "@/lib/chat/type";

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

export const AssistantBlockView = memo(function AssistantBlockView({ block }: { block: AssistantBlock }) {
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
      {typeof block.durationMs === "number" && (
        <p className="flex items-center gap-1.5 text-[11px] text-faint">
          <Clock className="size-3" />
          用时 {formatDuration(block.durationMs)}
        </p>
      )}
    </article>
  );
});
