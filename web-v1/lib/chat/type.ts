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

export type AssistantBlock = {
  type: "assistant";
  id: string;
  createdAt: string;
  blocks: AssistantContentBlock[];
  /** 本轮耗时（毫秒）。实时由终态事件写入；历史由后端持久化值投影（无记录时为 null） */
  durationMs?: number | null;
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
export type ChatStreamEvent =
  | RunStartedEvent
  | ContentDeltaEvent
  | ToolStartedEvent
  | ToolFinishedEvent
  | RunErrorEvent
  | RunCompletedEvent
  | RunCanceledEvent;

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
  /** 本轮耗时（毫秒），与后端落库值同源 */
  durationMs?: number;
};

export type RunCanceledEvent = {
  type: "run.canceled";
  requestId: string;
  assistantId?: string;
  sessionUsage?: SessionUsage;
  /** 本轮耗时（毫秒），与后端落库值同源 */
  durationMs?: number;
};
