// ETHOS's browser-facing conversation contract.
//
// Do not import or model trpc-agent-go events in the Web app. Live SSE events
// and historical session data are both projected into this timeline shape.

export type Timeline = {
  blocks: TimelineBlock[];
};

export type TimelineBlock = UserBlock | AssistantBlock | ErrorBlock;

export type UserBlock = {
  type: "user";
  id: string;
  content: string;
  images: UserImage[];
  createdAt: string;
};

// ChatImage is an ETHOS-owned image reference. The browser always loads it
// through /api/images/{id}; it never stores a short-lived COS URL.
export type ChatImage = {
  id: string;
  mimeType: string;
};

export type UserImage = {
  id: string;
};

// 完成证据等级：run 收尾时按**事实**判定，不看模型说了什么。
// mutated  本轮内置写类工具成功执行过（有产出）
// external MCP 工具（能力未知，不声称也不排除）
// read_only 仅只读工具（查了，但没改）
// none     零工具调用（可能只是回答，也可能是"假完成"）
export type CompletionEvidence = "mutated" | "external" | "metadata_only" | "read_only" | "none";

/**
 * 任务语义：只回答「完成了吗」，不回答「为什么停」。
 *
 * 与 endReason 分开是刻意的：原因会随后端实现增长（轮次耗尽 / 上下文预算 /
 * 输出截断 / 内容过滤 …），语义只有两个值。UI 只判语义，因此后端新增原因
 * 不需要改前端；原因只用来挑文案，并且必须有兜底。
 */
export type RunOutcome = "complete" | "incomplete";

export type AssistantBlock = {
  type: "assistant";
  id: string;
  createdAt: string;
  blocks: AssistantContentBlock[];
  /** 本轮耗时（毫秒）。实时由终态事件写入；历史由后端持久化值投影（无记录时为 null） */
  durationMs?: number | null;
  /** 完成证据等级。实时由终态事件写入；历史由后端持久化值投影。
   *  null / undefined = 未知（该字段引入前的旧消息），此时不要显示任何证据标记 */
  completionEvidence?: CompletionEvidence | null;
  /** 任务语义（complete/incomplete）。服务端派生，前端不参与判定。
   *  null / undefined = 未知（该字段引入前的旧消息），不显示未完成标记 */
  outcome?: RunOutcome | null;
  /** 终止原因，仅用于挑选提示文案；未知值必须走兜底文案 */
  endReason?: string | null;
  /** 产生这条回复的 run。计划（run.plan）按 run 归档，界面靠它把计划面板
   *  定位到所属的那一轮；null / undefined = 旧数据，不做计划定位 */
  requestId?: string | null;
};

export type ErrorBlock = {
  type: "error";
  id: string;
  message: string;
  createdAt: string;
};

export type AssistantContentBlock =
  | ReasoningBlock
  | TextBlock
  | ToolBlock;

export type ReasoningBlock = {
  type: "reasoning";
  id: string;
  content: string;
};

export type TextBlock = {
  type: "text";
  id: string;
  content: string;
};

export type ToolBlock = {
  type: "tool";
  id: string; // trpc-agent-go ToolCall.ID
  toolName: string;
  arguments: string;
  status: "running" | "completed" | "failed";
  result?: string;
};

// Browser → Next BFF. userId must never be supplied by the browser.
export type ChatRequest = {
	requestId: string;
	sessionId: string;
  message: string;
  imageIds: string[];
  uploadPaths: string[];
  modelId: string;
  reasoningOptionId?: string;
  workspaceId?: string;
};

export type AgentRunStatus = {
  requestId: string;
  status: "running";
};

export type CreateImageUploadRequest = {
  sessionId: string;
  mimeType: string;
  sizeBytes: number;
};

export type CreateImageUploadResponse = {
  image: ChatImage;
  uploadUrl: string;
};

export type Conversation = {
  id: string;
  title: string;
  updatedAt: string;
  workspaceId?: string; // 空 = 普通对话（不属于任何工作区）
  archived?: boolean; // 本次新增：是否已归档
};

export type ConversationListResponse = {
  conversations: Conversation[];
};

export type ConversationResponse = {
  timeline: Timeline;
  usage: SessionUsage;
  /** 该会话最近一次 run 的计划快照（刷新后回填计划面板）；从未建过计划时为 null */
  plan?: PlanSnapshot | null;
  /** 任务状态（in_progress/completed/canceled）；null = 从未有 run。
   *  这是「还有没跑完的活」的权威来源：刷新后仍然存在，
   *  不依赖 sessionStorage 里那份只属于当前标签页的 pending 记录。 */
  taskStatus?: string | null;
};

/** 计划项状态。四档是固定的：多一档就多一种「自己发明语义」的机会。 */
export type PlanItemStatus = "pending" | "in_progress" | "done" | "blocked";

export type PlanItem = {
  id: string;
  title: string;
  status: PlanItemStatus;
  /** true = 该 done 项的证据已通过服务端核对（模型无法自己写 true） */
  verified?: boolean | null;
  /** 服务端解析后的证据：命中哪次工具调用、哪个路径 / 退出码 */
  evidence?: {
    toolCallIds?: string[];
    path?: string;
    expectExitCode?: number;
  } | null;
};

export type PlanSnapshot = {
  requestId: string;
  /** 单调递增：乱序到达时丢弃旧版本，保证前端幂等覆盖 */
  revision: number;
  updatedAt?: string;
  items: PlanItem[];
};

/**
 * 会话用量汇总。
 *
 * 缓存类字段可能缺失（模型没上报）：SSE 走 exclude_none 会直接不带该字段，
 * 历史接口则给 null —— 两种都要按「没有」处理，不能用 undefined/null 参与算术。
 * totalRuns / cacheReportedRuns / estimatedRuns 说明数字覆盖了多少次运行。
 */
export type SessionUsage = {
  totalTokens: number;
  cachedPromptTokens: number | null;
  uncachedPromptTokens: number | null;
  completionTokens: number;
  cacheHitRate: number | null;
  totalRuns: number;
  cacheReportedRuns: number;
  estimatedRuns: number;
};

export const emptySessionUsage: SessionUsage = {
  totalTokens: 0,
  cachedPromptTokens: null,
  uncachedPromptTokens: null,
  completionTokens: 0,
  cacheHitRate: null,
  totalRuns: 0,
  cacheReportedRuns: 0,
  estimatedRuns: 0,
};

// Next BFF → Agent Gateway. userId comes from Clerk on the BFF.
export type GatewayMessageRequest = ChatRequest & {
  userId: string;
};

// Channel-neutral Agent Gateway events. Browser code projects these into its
// Timeline; IM adapters can render the same events as cards or messages.
export type ChatStreamEvent = (
  | RunStartedEvent
  | ContentDeltaEvent
  | ToolStartedEvent
  | ToolFinishedEvent
  | PlanUpdatedEvent
  | UsageUpdatedEvent
  | RunLogTruncatedEvent
  | RunErrorEvent
  | RunCompletedEvent
  | RunCanceledEvent
) & {
  /**
   * 事件在该 run 内的单调序号（1 起）。断流重连时作为 `after` 回传给服务端，
   * 补发之后的事件——这是「刷新页面后立刻看到已发生的执行过程」的全部依据。
   * 旧后端不带这个字段，此时按 0 处理：拿不到增量就退回整份重来。
   */
  seq?: number;
};

/**
 * 服务端日志断层：本次 run 的事件太多，`after` 之前的部分已被丢弃。
 * 收到它说明「接着补发」不可能完整，只能退回拉历史对齐。
 */
export type RunLogTruncatedEvent = {
  type: "run.log_truncated";
  requestId?: string;
  sessionId?: string;
  /** 已被丢弃的最小 seq（十进制字符串，复用 delta 字段避免新增载荷） */
  delta?: string;
};

/** 计划快照更新：整份携带，前端按 revision 覆盖。 */
export type PlanUpdatedEvent = {
  type: "plan.updated";
  requestId: string;
  sessionId?: string;
  plan: PlanSnapshot;
};

/**
 * 用量更正：终态事件之后，收尾阶段的附属调用（自动记忆抽取）也是一次真实
 * 模型调用，账要补记。补记发生在 run.completed 之后，所以单开一个事件，
 * 让「实时看到的用量」和「刷新后的用量」仍是同一个数（值来自 DB，不在前端重算）。
 */
export type UsageUpdatedEvent = {
  type: "usage.updated";
  requestId: string;
  sessionId?: string;
  sessionUsage: SessionUsage;
};

export type RunStartedEvent = {
  type: "run.started";
  sessionId: string;
  requestId: string;
  assistantId: string;
};

export type ContentDeltaEvent = {
  type: "reasoning.delta" | "message.delta";
  assistantId: string;
  blockId: string;
  blockType: "reasoning" | "text";
  delta: string;
};

export type ToolStartedEvent = {
  type: "tool.started";
  assistantId: string;
  toolCallId: string;
  toolName: string;
  arguments?: string;
  toolStatus: "running";
};

export type ToolFinishedEvent = {
  type: "tool.finished";
  assistantId: string;
  toolCallId: string;
  toolStatus: "completed" | "failed";
  toolResult?: string;
};

export type GatewayError = {
  type: string;
  message: string;
};

export type RunErrorEvent = {
  type: "run.error";
  requestId: string;
  assistantId?: string;
  /** 本轮耗时（毫秒）：run.started → 出错为止 */
  durationMs?: number;
  error: GatewayError;
};

export type RunCompletedEvent = {
  type: "run.completed";
  requestId: string;
  assistantId?: string;
  sessionUsage?: SessionUsage;
  /** 终止原因：completed=模型主动收尾；rounds_exhausted=工具轮次耗尽未完成 */
  endReason?: string;
  /** 任务语义（由服务端派生）：只判「完成/未完成」，不要枚举 endReason */
  outcome?: RunOutcome;
  /** 本轮耗时（毫秒），与后端落库值同源 */
  durationMs?: number;
  /** 完成证据等级，与后端落库值同源。缺字段 = 未知（旧后端/旧消息） */
  completionEvidence?: CompletionEvidence;
  /** 最终计划快照（与 plan.updated 同源）：即使中间事件丢包，终态也能对齐 */
  plan?: PlanSnapshot | null;
};

export type RunCanceledEvent = {
  type: "run.canceled";
  requestId: string;
  assistantId?: string;
  sessionUsage?: SessionUsage;
  /** 本轮耗时（毫秒），与后端落库值同源 */
  durationMs?: number;
};
