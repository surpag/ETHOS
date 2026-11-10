import { memo, useEffect, useRef } from "react";

import type { PlanSnapshot, Timeline } from "@/lib/chat/type";

import { AssistantBlockView } from "./assistant-block";
import { PlanPanel } from "./plan-panel";
import { UserBlockView } from "./user-block";

/**
 * 会话级横幅只有两种：都在说「后台还在跑，只是你看不到实时输出」。
 *
 * 「任务未完成」不在这里 —— 它是**消息级**的持久状态（AssistantBlock.outcome），
 * 挂在某条回复下面。放在这里会随着 React state 一起丢失（刷新即消失），
 * 也会和「正在执行」用同一套呼吸动画，让人分不清到底停没停。
 */
export type TimelineRunNotice =
  | "stream_disconnected"
  | "session_busy"
  | "workspace_busy"
  | "duplicate_request";

type TimelineViewProps = {
  timeline: Timeline;
  runNotice?: TimelineRunNotice;
  /** 会话还有未完成的任务，但最后一条回复没有承载「未完成」标记（如中断在工具执行中） */
  pendingTask?: boolean;
  /** 本次运行的计划：属于这段对话，跟着内容一起滚，不钉在输入框上方 */
  plan?: PlanSnapshot | null;
  /** 本会话是否还在执行（计划面板据此决定是否保持展开） */
  planRunning?: boolean;
};

export const TimelineView = memo(function TimelineView({
  timeline,
  runNotice,
  pendingTask,
  plan,
  planRunning,
}: TimelineViewProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // 用户停留在底部时自动跟随新消息；向上翻阅历史时暂停跟随。
  const stickToBottom = useRef(true);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container || !stickToBottom.current) return;
    container.scrollTop = container.scrollHeight;
    // 计划快照的 revision 也要算变化：它是流内内容的一部分，长高了同样需要跟随，
    // 否则计划多出几行会把最后一条消息顶出可视区。
  }, [timeline, plan?.revision]);

  function handleScroll() {
    const container = scrollRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    stickToBottom.current = distanceFromBottom < 80;
  }
  // 计划属于**产生它的那一轮**，而且只有「最近一轮」才显示：
  //   · 挂到别的轮次下面 = 上一轮的计划看着像这一轮的（就是「新对话沿用旧计划」）；
  //   · 全部轮次都留着 = 用户发了新消息还看到上一轮的计划，误以为当前在跑它。
  // 任务完成后计划留着（那是这一轮的收尾总结），一旦发出新消息、新的一轮出现，
  // 它就随旧轮次一起退出视野；新一轮要是有计划，会自己 emit 一份新的。
  const lastAssistantID = [...timeline.blocks]
    .reverse()
    .find((block) => block.type === "assistant")?.id;
  return (
    <div className="min-h-0 flex-1 overflow-y-auto" onScroll={handleScroll} ref={scrollRef}>
      <div className="mx-auto max-w-4xl space-y-7 px-6 py-8 md:px-8">
        {timeline.blocks.map((block, index) => {
          if (block.type === "user") {
            return <UserBlockView block={block} key={block.id ?? `block-${index}`} />;
          }
          if (block.type === "assistant") {
            const ownsPlan =
              Boolean(plan) &&
              block.id === lastAssistantID &&
              plan?.requestId === block.requestId;
            return (
              <div className="space-y-3" key={block.id ?? `block-${index}`}>
                <AssistantBlockView block={block} />
                {ownsPlan && plan && <PlanPanel plan={plan} running={planRunning} />}
              </div>
            );
          }
          return (
            <p className="text-sm text-danger" key={block.id ?? `block-${index}`}>
              {block.message}
            </p>
          );
        })}
        {pendingTask && <PendingTaskNotice />}
        {runNotice && <ActiveRunNotice reason={runNotice} />}
      </div>
    </div>
  );
});

/**
 * 「还有没跑完的活」：状态来自后端的 session_meta.task_status（权威），
 * 不来自前端内存。所以刷新、换标签页、换设备都能看到。
 * 只有在该会话没有正在执行的 run 时才显示（正在跑时由输入框与工具卡片表达）。
 */
function PendingTaskNotice() {
  return (
    <div
      className="flex items-center gap-3 rounded-xl bg-warning-soft px-4 py-3 text-sm"
      role="status"
    >
      <span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-warning" />
      <div>
        <p className="font-medium text-warning">此会话有未完成的任务</p>
        <p className="mt-0.5 text-xs text-warning/80">发送「继续」可从上次的进度接着执行。</p>
      </div>
    </div>
  );
}

function ActiveRunNotice({ reason }: { reason: TimelineRunNotice }) {
  // 每种横幅的「后台到底在跑什么」不同，文案必须能导出一个具体动作：
  // 等自己 / 等这个工作区 / 切个会话继续 / 重新发一次。共用一套文案会让人误判。
  const copy = reason === "stream_disconnected"
    ? {
        // 现在断流后会立刻自动重连（带 seq 续订），这条横幅只在「重连没成功」
        // 时出现，所以文案要说清楚「我在重试」而不是「你只能等」。
        title: "正在重新连接实时输出",
        description: "任务仍在后台执行，会自动补上错过的执行过程；也可以先等着，完成后自动加载完整结果。",
      }
    : reason === "workspace_busy"
      ? {
          title: "该工作区正被另一个会话使用",
          description: "同一工作区共用一份文件，两个会话同时写会互相覆盖；等它结束，或切到别的工作区再发。",
        }
      : reason === "duplicate_request"
        ? {
            // request_conflict：同一个 requestId 被重复提交（重试/回放）。
            // 说成「会话正在生成回复」是错的——用户会去等一个已经结束的任务。
            title: "这条消息已经提交过了",
            description: "服务端收到了重复的请求标识；这条消息没有被重复执行，重新发送即可。",
          }
        : {
            title: "该会话正在生成回复",
            description: "已有一条任务正在执行；完成后会自动显示完整结果。",
          };

  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-surface-subtle px-4 py-3 text-sm text-muted" role="status">
      <span aria-hidden className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-accent" />
      <div>
        <p className="font-medium text-ink">{copy.title}</p>
        <p className="mt-0.5 text-xs text-muted">{copy.description}</p>
      </div>
    </div>
  );
}
