import { CheckCircle2, ChevronDown, Circle, CircleDot, CircleSlash, ListChecks } from "lucide-react";
import { memo, useState } from "react";

import type { PlanItem, PlanItemStatus, PlanSnapshot } from "@/lib/chat/type";

// 计划面板：**属于当前这段对话**的一块内容，跟着消息一起滚。
//
// 为什么不再钉在输入框上方：常驻区块在长会话里会一直占着最贵的那块屏幕，
// 而且用完也不消失——任务早就完成了，「3 已完成 · 0 进行中」还挂在那里挡住
// 正文。计划是这一轮任务的产物，不是全局状态栏，所以它应该：
//   ① 出现在对话流里（滚上去就看不见）；
//   ② 全部做完之后自动收起成一行（留下结论，不占地方）。
//
// 它显示的不是「模型说做完了」，而是「有调用能证明做完了」：每一行的状态都
// 来自结构化快照，done 项必须带服务端核对过的证据。

const STATUS_META: Record<
  PlanItemStatus,
  { icon: typeof Circle; label: string; className: string }
> = {
  pending: { icon: Circle, label: "待处理", className: "text-faint" },
  in_progress: { icon: CircleDot, label: "进行中", className: "text-accent" },
  done: { icon: CheckCircle2, label: "已完成", className: "text-success" },
  blocked: { icon: CircleSlash, label: "受阻", className: "text-warning" },
};

const STATUS_ORDER: PlanItemStatus[] = ["in_progress", "blocked", "pending", "done"];

function evidenceHint(item: PlanItem): string {
  const evidence = item.evidence;
  if (!evidence) {
    return item.verified ? "已核实" : "已完成";
  }
  if (evidence.path) {
    return `已核实：${evidence.path}`;
  }
  if (typeof evidence.expectExitCode === "number") {
    return `已核实：命令退出码 ${evidence.expectExitCode}`;
  }
  return item.verified ? "已核实" : "已完成";
}

export const PlanPanel = memo(function PlanPanel({
  plan,
  running = false,
}: {
  plan?: PlanSnapshot | null;
  /** 本会话是否还在执行：执行中永远展开，这是「现在做到哪了」的唯一出口 */
  running?: boolean;
}) {
  // null = 跟随默认策略；用户点过之后以用户的选择为准。
  // 之所以允许「默认策略」动态变化（而不是初始化一次就定死）：任务跑完之后
  // 它必须能自己收起来，否则就成了永久占位的横幅。
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const items = plan?.items ?? [];
  if (items.length === 0) {
    return null;
  }

  const counts = items.reduce<Record<string, number>>((acc, item) => {
    acc[item.status] = (acc[item.status] ?? 0) + 1;
    return acc;
  }, {});
  const ordered = [...items].sort(
    (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status),
  );
  const verifiedCount = items.filter((item) => item.verified).length;
  const done = counts.done ?? 0;
  const unfinished =
    (counts.pending ?? 0) + (counts.in_progress ?? 0) + (counts.blocked ?? 0);
  const expanded = manualOpen ?? (running || unfinished > 0);

  return (
    <div className="rounded-xl border border-border bg-surface">
      <button
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[11px] font-medium text-muted transition-colors hover:text-ink"
        onClick={() => setManualOpen(!expanded)}
        type="button"
      >
        <ListChecks className="size-3.5 shrink-0 text-faint" />
        <span className="shrink-0">执行计划</span>
        <span className="min-w-0 truncate text-faint">
          {done}/{items.length} 已完成
          {counts.in_progress ? ` · ${counts.in_progress} 进行中` : ""}
          {counts.blocked ? ` · ${counts.blocked} 受阻` : ""}
          {done > 0 ? `（${verifiedCount} 项有工具调用可核对）` : ""}
        </span>
        {running && (
          <span
            aria-label="执行中"
            className="size-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-accent/25 border-t-accent"
          />
        )}
        <ChevronDown
          className={`ml-auto size-3.5 shrink-0 text-faint transition-transform ${expanded ? "rotate-180" : ""}`}
        />
      </button>
      {expanded && (
        <ul className="flex flex-col gap-1 border-t border-border px-3 py-2">
          {ordered.map((item) => {
            const meta = STATUS_META[item.status] ?? STATUS_META.pending;
            const Icon = meta.icon;
            return (
              <li
                className="flex items-start gap-2 text-[12.5px] leading-5 text-muted"
                key={item.id}
                title={evidenceHint(item)}
              >
                <Icon className={`mt-0.5 size-3.5 shrink-0 ${meta.className}`} />
                <span
                  className={
                    item.status === "done"
                      ? "text-faint line-through decoration-border-strong"
                      : undefined
                  }
                >
                  {item.title}
                </span>
                {item.status === "done" && !item.verified && (
                  // 正常路径不会出现：done 没有证据提交不进来。留着是为了让异常可见。
                  <span className="shrink-0 rounded bg-warning-soft px-1 text-[10px] text-warning">
                    未核实
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
});
