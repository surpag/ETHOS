"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";

import { AccountMenu } from "@/components/account-menu";
import { AppSidebar } from "@/components/app-sidebar";
import { SettingsDialog } from "@/components/settings/settings-dialog";
import { emptySessionUsage } from "@/lib/chat/type";
import { fetchActiveSessionID } from "@/lib/chat/workspace-session";
import { applyStreamEvent, errorTimelineEvent, readChatStream } from "@/lib/chat/stream";
import type {
  AgentRunStatus,
	ConversationListResponse,
  ConversationResponse,
  PlanSnapshot,
  SessionUsage,
  Timeline,
  UserBlock,
} from "@/lib/chat/type";
import type { AvailableModelCatalogResponse, ModelInfo } from "@/lib/models/type";
import { subscribeModelsChanged } from "@/lib/models/events";

import { ChatComposer } from "./chat-composer";
import { ConversationList } from "./conversation-list";
import { TimelineView, type TimelineRunNotice } from "./timeline";
import { SandboxPanel } from "./sandbox-panel";
import { SessionUsageView } from "./session-usage";
import { WorkspaceSwitcher, type WorkspaceSwitcherHandle } from "./workspace-switcher";

type ChatSession = {
  id: string;
  title: string;
  timeline: Timeline;
  usage: SessionUsage;
  workspaceId?: string; // 会话归属的工作区，空 = 普通对话
  archived?: boolean; // 本次新增：是否已归档
  /** 该会话最近的计划快照（run 级状态，不是 Timeline 的一部分） */
  plan?: PlanSnapshot | null;
  /** 任务状态（in_progress/completed/canceled）；来自后端 session_meta，刷新后仍在 */
  taskStatus?: string | null;
};

type Workspace = {
  workspaceId: string;
  name: string;
  metadata?: {
    folderName?: string;
    fileCount?: number;
  };
};

const emptyTimeline: Timeline = { blocks: [] };

// 占位会话：新用户还没有任何会话时用它撑住渲染。
// 必须是稳定引用 —— 它同时作为 ChatComposer 的 key，每次新建对象会导致反复重挂载。
const emptySession: ChatSession = {
  id: "new-session",
  title: "新对话",
  timeline: emptyTimeline,
  usage: emptySessionUsage,
};

const pendingRunsKey = "edith.pending-agent-runs";

// 计划快照合并：同一 request 内只接受更新的 revision（SSE 可能乱序/重放）；
// request 变了说明是新的一次 run，直接采用。
function mergePlan(
  current: PlanSnapshot | null | undefined,
  incoming: PlanSnapshot | null | undefined,
): PlanSnapshot | null | undefined {
  if (!incoming) return current;
  if (
    current &&
    current.requestId === incoming.requestId &&
    incoming.revision < current.revision
  ) {
    return current;
  }
  return incoming;
}

type PendingRun = {
  requestId: string;
  sessionId: string;
  /**
   * 该 run 已收到的事件序号上界。刷新页面后带着它回来重连，服务端只补发
   * 之后的——这就是「刷新后立刻看到已发生的执行过程」的全部依据。
   * 缺省（旧记录）按 0 处理：从头发一遍，代价是重放多一点，但不会漏。
   */
  lastSeq?: number;
};

/**
 * 最后一条 assistant 回复是否已经承载了「未完成」标记。
 *
 * 会话级横幅（taskStatus=in_progress）和消息级标记（outcome=incomplete）说的是
 * 同一件事，只是粒度不同：消息级能解释「这一轮为什么停」，会话级能覆盖
 * 「中断在工具执行中、连回复都没落库」的情况。两者同时出现就是重复提示，
 * 所以会话级只在消息级没覆盖时才显示。
 */
function hasIncompleteTail(timeline: Timeline): boolean {
  for (let index = timeline.blocks.length - 1; index >= 0; index -= 1) {
    const block = timeline.blocks[index];
    if (block.type === "assistant") {
      return block.outcome === "incomplete";
    }
  }
  return false;
}

function loadPendingRuns(): PendingRun[] {
  const value = sessionStorage.getItem(pendingRunsKey);
  if (!value) return [];

  try {
    const pendingRuns = JSON.parse(value) as unknown;
    if (!Array.isArray(pendingRuns)) return [];
    return pendingRuns.filter((run): run is PendingRun =>
      typeof run === "object" && run !== null &&
      "requestId" in run && typeof run.requestId === "string" &&
      "sessionId" in run && typeof run.sessionId === "string",
    );
  } catch {
    return [];
  }
}

function savePendingRun(pendingRun: PendingRun) {
  const pendingRuns = loadPendingRuns().filter((run) => run.requestId !== pendingRun.requestId);
  sessionStorage.setItem(pendingRunsKey, JSON.stringify([...pendingRuns, pendingRun]));
}

function removePendingRun(requestId: string) {
  const pendingRuns = loadPendingRuns().filter((run) => run.requestId !== requestId);
  sessionStorage.setItem(pendingRunsKey, JSON.stringify(pendingRuns));
}

// ── 事件序号 ────────────────────────────────────────────────────────────────
// 每条 SSE 事件带 run 内单调 seq。把它记到 sessionStorage，刷新后才能告诉服务端
// 「我已经看到第 N 条，后面的补给我」。不记的话只能从头重放，长任务会一次性
// 涌进几千条。
//
// 写入必须节流：sessionStorage 是同步 IO，且每次都要序列化整个数组——按 delta
// 频率写会让长回复明显卡顿。用尾部延迟写（400ms）保证最后一次一定落地；
// 代价是刷新正好卡在两次写之间时会重放几百毫秒的内容，因为应用是幂等的，无害。
const seqBuffers = new Map<string, number>();
const seqTimers = new Map<string, number>();
const SEQ_FLUSH_MS = 400;

function flushStreamSeq(requestId?: string) {
  const targets = requestId ? [requestId] : [...seqBuffers.keys()];
  for (const id of targets) {
    const value = seqBuffers.get(id);
    const timer = seqTimers.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      seqTimers.delete(id);
    }
    if (value === undefined) continue;
    seqBuffers.delete(id);
    const pendingRuns = loadPendingRuns();
    let changed = false;
    const next = pendingRuns.map((run) => {
      if (run.requestId !== id) return run;
      changed = true;
      return { ...run, lastSeq: value };
    });
    if (changed) sessionStorage.setItem(pendingRunsKey, JSON.stringify(next));
  }
}

function rememberStreamSeq(requestId: string, seq: number) {
  const previous = seqBuffers.get(requestId);
  if (previous !== undefined && seq <= previous) return;
  seqBuffers.set(requestId, seq);
  if (seqTimers.has(requestId)) return;
  seqTimers.set(requestId, window.setTimeout(() => flushStreamSeq(requestId), SEQ_FLUSH_MS));
}

function readStreamSeq(requestId: string): number {
  return seqBuffers.get(requestId)
    ?? loadPendingRuns().find((run) => run.requestId === requestId)?.lastSeq
    ?? 0;
}

function messageWithUploads(content: string, uploadPaths: string[]) {
  const message = content.trim();
  if (uploadPaths.length === 0) return message;
  const files = uploadPaths.map((path) => `- \`${path.split("/").at(-1) || path}\``).join("\n");
  const attachments = `附件：\n${files}`;
  return message ? `${message}\n\n${attachments}` : attachments;
}

export function ChatPage() {
  // 运行态按会话存放（sessionID → requestID）。用全局布尔值时，A 会话在跑会让
  // B 会话的发送按钮直接失效，B 的「停止」按钮取消的却是 A 的任务 —— 两个都是
  // 单值与多会话语义不匹配造成的。后端本来就允许不同会话并发（同会话才互斥）。
  const [runningBySession, setRunningBySession] = useState<Record<string, string>>({});
  const [cancellingBySession, setCancellingBySession] = useState<Record<string, boolean>>({});
  const [isLoadingConversations, setIsLoadingConversations] = useState(true);
  const [sessions, setSessions] = useState<ChatSession[]>([emptySession]);
  const [activeSessionID, setActiveSessionID] = useState("new-session");
  const [activeWorkspaceID, setActiveWorkspaceID] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelID, setModelID] = useState("");
  const [reasoningOptionID, setReasoningOptionID] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sandboxOpen, setSandboxOpen] = useState(false);
  // 提示也按会话分槽：原来只有一个槽位，两个会话同时断流时后一个会覆盖前一个，
  // 被覆盖的那个会话就变成「看起来正常、其实没连上」。
  const [sessionRunNotices, setSessionRunNotices] = useState<Record<string, TimelineRunNotice>>({});
  const liveStreamSessionIDs = useRef(new Set<string>());
  // 正在重连中的 requestID。状态轮询每 3 秒跑一次，若不挡住会叠加多条
  // 重连请求——同一个 run 会因此被订阅多次，事件被重复应用。
  const resumeInFlightRef = useRef(new Set<string>());
  // 状态轮询的 effect 只跑一次（空依赖），但它要调用每次渲染都会重建的
  // resumeRunStream。用 ref 转一道：既拿到最新闭包，又不让轮询跟着重挂。
  const resumeRunStreamRef = useRef<
    (sessionID: string, requestID: string, showNoticeOnFailure: boolean) => Promise<boolean>
  >(async () => false);
  const abortBySessionRef = useRef(new Map<string, AbortController>());
  const requestBySessionRef = useRef(new Map<string, string>());
  // 异步路径（状态轮询、断流重连）拿不到本次渲染的 sessions 闭包，但它们必须
  // 知道「事件有没有落点」。用 ref 镜像一份：只读，不参与渲染决策。
  const sessionsRef = useRef<ChatSession[]>(sessions);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);
  const workspaceSwitcherRef = useRef<WorkspaceSwitcherHandle>(null); // 本次新增：侧边栏"+ 添加工作区"按钮复用 WorkspaceSwitcher 的文件选择

  // 兜底到 emptySession：sessions 理论上不会为空（初始就有占位），但一旦为空，
  // 下面所有 `activeSession.xxx` 都会抛 TypeError → Next.js 全局错误页（无 digest）。
  const activeSession = sessions.find((session) => session.id === activeSessionID) ?? sessions[0] ?? emptySession;
  const selectedModel = models.find((model) => model.id === modelID);
  // 当前会话的运行态：所有「发送 / 停止 / 提示」判断都只看它，不看别的会话。
  const activeRequestID = runningBySession[activeSession.id] ?? null;
  const activeRunning = activeRequestID !== null;
  const isCancelling = Boolean(cancellingBySession[activeSession.id]);
  // 侧边栏用：哪些会话正在跑（行尾旋转环 + 底部汇总都从这一份派生）
  const runningSessionIDs = Object.keys(runningBySession);
  const otherRunningCount = runningSessionIDs.filter((id) => id !== activeSession.id).length;

  // 会话还有没跑完的活：taskStatus 是后端的权威状态（刷新后仍在）。
  // 三个前提缺一不可：
  //   1. 当前没有正在执行的 run（在跑时由输入框与工具卡片表达，不需另说）；
  //   2. 当前会话没有别的横幅（否则两条提示叠在一起）；
  //   3. 最后一条回复没承载「未完成」标记（有的话消息级已经说清楚了）。
  const pendingTask =
    activeSession.taskStatus === "in_progress" &&
    !activeRunning &&
    sessionRunNotices[activeSession.id] === undefined &&
    !hasIncompleteTail(activeSession.timeline);

  // 从 localStorage 恢复当前工作区 ID（顶部切换器写入）；没有则工作区指令不注入。
  useEffect(() => {
    setActiveWorkspaceID(localStorage.getItem("ethos.workspaceId") ?? null);
  }, []);

  // 页面即将卸载时把还没落地的序号刷进 sessionStorage。不刷的话，刷新前
  // 最后 400ms 的事件会在重连时被重放——幂等，所以只是多渲染一点，
  // 但能免则免：重连的第一屏越干净越好。
  useEffect(() => {
    const flush = () => flushStreamSeq();
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  // 加载工作区列表（供侧边栏分组展示；WorkspaceSwitcher 增删后经 onWorkspacesChanged 触发刷新）
  async function loadWorkspaces() {
    const response = await fetch("/api/workspaces");
    if (!response.ok) return;
    const value = (await response.json()) as { workspaces: Workspace[] };
    setWorkspaces(value.workspaces);
  }
  useEffect(() => {
    void loadWorkspaces();
  }, []);

  // 标记某个会话开始运行。用 ref 先判重：页面刷新后的状态轮询每 3 秒会重复报告
  // 「仍在运行」，不能每次都 setState，否则整页每 3 秒白重渲染一次。
  function markRunning(sessionID: string, requestID: string) {
    if (requestBySessionRef.current.get(sessionID) === requestID) return;
    requestBySessionRef.current.set(sessionID, requestID);
    setRunningBySession((current) => ({ ...current, [sessionID]: requestID }));
  }

  // 结束某个会话的运行态。requestID 必须匹配：A 的旧流收尾时不能把 B 刚置上的
  // 运行态清掉（同一会话串行，但不同会话的结束事件会交错到达）。
  function finishRun(sessionID: string, requestID: string) {
    if (requestBySessionRef.current.get(sessionID) !== requestID) return;
    requestBySessionRef.current.delete(sessionID);
    abortBySessionRef.current.delete(sessionID);
    setRunningBySession((current) => {
      if (current[sessionID] !== requestID) return current;
      const next = { ...current };
      delete next[sessionID];
      return next;
    });
    setCancellingBySession((current) => {
      if (!current[sessionID]) return current;
      const next = { ...current };
      delete next[sessionID];
      return next;
    });
  }

  function clearSessionRunNotice(sessionID: string) {
    setSessionRunNotices((current) => {
      if (!(sessionID in current)) return current;
      const next = { ...current };
      delete next[sessionID];
      return next;
    });
  }

  function setSessionRunNotice(sessionID: string, reason: TimelineRunNotice) {
    setSessionRunNotices((current) => ({ ...current, [sessionID]: reason }));
  }

  /**
   * 消费一个 run 的事件流。首连与重连走同一条路径——这正是重连能「无缝接上」
   * 的原因：重连不是另一套渲染逻辑，只是同一个流从 `after=N` 开始读。
   *
   * 返回是否正常收到终态事件。重连时若拿不到终态（服务端日志过期/进程重启），
   * 保持运行态并交由状态轮询兜底，而不是假装这一轮结束了。
   */
  async function consumeRunStream(
    sessionID: string,
    requestID: string,
    response: Response,
    options: { resumed?: boolean } = {},
  ): Promise<boolean> {
    let streamCompleted = false;
    let truncated = false;
    try {
      await readChatStream(response, (event) => {
        // 先记序号再应用：即使下面某个分支抛错，断点也是准的。
        if (typeof event.seq === "number") rememberStreamSeq(requestID, event.seq);
        if (event.type === "run.log_truncated") {
          // 服务端说「after 之前那一段已经被丢弃」：这次补发不完整，
          // 流结束后拉一次历史做权威对齐，不要把缺口留在屏幕上。
          truncated = true;
          return;
        }
        if (event.type === "run.completed" || event.type === "run.canceled") {
          streamCompleted = true;
          flushStreamSeq(requestID);
          removePendingRun(requestID);
          finishRun(sessionID, requestID);
        }
        setSessions((current) => current.map((session) =>
          session.id === sessionID
            ? {
                ...session,
                timeline: applyStreamEvent(session.timeline, event),
                // 用量只认后端给的值：
                //  - run.completed/canceled 带本轮累计；
                //  - usage.updated 是收尾阶段附属调用（记忆抽取）的补记，
                //    发生在终态事件之后。把它也写进来，实时值与刷新后的值才同源。
                usage:
                  event.type === "usage.updated"
                    ? event.sessionUsage
                    : (event.type === "run.completed" || event.type === "run.canceled") &&
                        event.sessionUsage
                      ? event.sessionUsage
                      : session.usage,
                // 计划是会话级状态：增量事件与终态快照走同一条合并规则，
                // 保证「实时看到的」和「刷新后回填的」是同一份（revision 最大者）。
                plan: mergePlan(
                  session.plan,
                  event.type === "plan.updated"
                    ? event.plan
                    : event.type === "run.completed"
                      ? event.plan
                      : undefined,
                ),
                // 任务状态跟语义位同步，规则与后端 set_task_status 完全一致：
                // 只有「完成」落 completed，其余一律留在 in_progress（可续跑）。
                // 用 outcome 判断而不是枚举 endReason：后端新增终止原因时这里不用改。
                taskStatus:
                  event.type === "run.completed"
                    ? (event.outcome === "complete" ? "completed" : "in_progress")
                    : event.type === "run.canceled"
                      ? "canceled"
                      : session.taskStatus,
              }
            : session,
        ));
      });
      if (!streamCompleted) {
        throw new Error("网络连接已断开，任务仍在后台继续。");
      }
      if (truncated) {
        await alignWithHistory(sessionID);
      }
      return true;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setSessionRunNotice(sessionID, "stream_disconnected");
      }
      return false;
    } finally {
      liveStreamSessionIDs.current.delete(sessionID);
      const aborted = abortBySessionRef.current.get(sessionID)?.signal.aborted ?? false;
      if (!aborted) {
        // 首连失败：清运行态，让发送按钮恢复（pending 仍在，轮询会重新点亮）。
        // 重连失败：不清——run 确实还在跑，清掉会让侧边栏的运行环闪断。
        if (streamCompleted || !options.resumed) {
          finishRun(sessionID, requestID);
        }
      }
    }
  }

  /**
   * 断流重连：带着「已收到的最大 seq」回到服务端，补发错过的部分再接实时。
   *
   * 这是刷新页面后能立刻看到执行过程的关键。之前的做法是只标记「当前页面未连接
   * 实时输出」并每 3 秒轮询状态，等于把用户按在黑洞里等 run 结束——而后端其实
   * 一直有完整的事件日志。
   *
   * 返回是否接上。接不上（404：日志过期 / 进程重启过 / run 从未存在）时由
   * 调用方退回「等结束拉历史」，那是唯一诚实的退路。
   */
  /**
   * 保证目标会话已经在 state 里存在——事件要有地方落。
   *
   * 刷新页面的瞬间 `sessions` 还是占位会话（列表请求还没回来），此时把补发的
   * 事件 `map` 上去会「查无此会话」而被静默丢弃。这不是网络问题，是顺序问题：
   * 先有容器，才有内容。列表接口是「会话存在」的权威来源，等它一次。
   */
  async function ensureSessionLoaded(sessionID: string): Promise<boolean> {
    if (sessionsRef.current.some((session) => session.id === sessionID)) return true;
    try {
      const list = await refreshConversations();
      return list.some((session) => session.id === sessionID);
    } catch {
      return false;
    }
  }

  async function resumeRunStream(
    sessionID: string,
    requestID: string,
    showNoticeOnFailure: boolean,
  ): Promise<boolean> {
    if (resumeInFlightRef.current.has(requestID) || liveStreamSessionIDs.current.has(sessionID)) {
      return true; // 已经在重连/已接上，轮询不要叠加请求
    }
    // 容器还没准备好的话，先等它——否则这一整轮补发都会掉进虚空。
    if (!(await ensureSessionLoaded(sessionID))) {
      if (showNoticeOnFailure) setSessionRunNotice(sessionID, "stream_disconnected");
      return false;
    }
    resumeInFlightRef.current.add(requestID);
    liveStreamSessionIDs.current.add(sessionID);
    markRunning(sessionID, requestID);
    // 重连也挂一个 AbortController：用户点「停止」时要能立刻断开这条流，
    // 而不是等服务端把 run 取消完再把流关掉。
    const abort = new AbortController();
    abortBySessionRef.current.set(sessionID, abort);

    let response: Response;
    try {
      const after = readStreamSeq(requestID);
      response = await fetch(
        `/api/agent-runs/${encodeURIComponent(requestID)}/events?after=${after}`,
        { cache: "no-store", signal: abort.signal },
      );
    } catch {
      resumeInFlightRef.current.delete(requestID);
      liveStreamSessionIDs.current.delete(sessionID);
      if (showNoticeOnFailure && !abort.signal.aborted) {
        setSessionRunNotice(sessionID, "stream_disconnected");
      }
      return false;
    }

    if (!response.ok) {
      resumeInFlightRef.current.delete(requestID);
      liveStreamSessionIDs.current.delete(sessionID);
      if (showNoticeOnFailure) setSessionRunNotice(sessionID, "stream_disconnected");
      return false;
    }

    // 接上了：把「未连接」的横幅撤掉。它描述的是上一刻的状态，
    // 而这一刻起用户又能看到实时进度了，留着就是自相矛盾。
    clearSessionRunNotice(sessionID);
    try {
      await consumeRunStream(sessionID, requestID, response, { resumed: true });
    } finally {
      resumeInFlightRef.current.delete(requestID);
    }
    return true;
  }

  /** 拉一次权威历史（用于日志断层后的对齐，或 run 已结束时的收尾）。 */
  async function alignWithHistory(sessionID: string) {
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(sessionID)}`);
      if (!response.ok) return;
      const conversation = await response.json() as ConversationResponse;
      setSessions((current) => current.map((session) =>
        session.id === sessionID
          ? {
              ...session,
              timeline: conversation.timeline,
              usage: conversation.usage,
              // 计划与任务状态同样来自历史：断流期间产生的计划/结论不能只活在
              // SSE 里，否则「重连失败」就等于「这一轮凭空消失」。
              plan: conversation.plan ?? session.plan ?? null,
              taskStatus: conversation.taskStatus ?? null,
            }
          : session,
      ));
    } catch {
      // 对齐失败不影响已经渲染的内容：它只是补充，不是唯一来源。
    }
  }

  // 每次渲染把最新的重连函数挂到 ref 上，供只跑一次的状态轮询调用。
  useEffect(() => {
    resumeRunStreamRef.current = resumeRunStream;
  });

  useEffect(() => {
    let stopped = false;

    async function loadAvailableModels() {
      const response = await fetch("/api/available-models");
      if (!response.ok || stopped) return;
      const catalog = await response.json() as AvailableModelCatalogResponse;
      setModels(catalog.models);
      // 选中项可能已被删除（自定义模型可删）：失效就回落到第一个可用模型，
      // 否则用户会拿一个不存在的 modelId 发消息，后端只回 "unsupported modelId"。
      setModelID((current) => {
        if (current && catalog.models.some((model) => model.id === current)) return current;
        return catalog.models[0]?.id ?? "";
      });
    }

    void loadAvailableModels();
    // 设置页新增/删除模型、保存供应商 Key 后广播一次 → 这里重新拉取，
    // 用户不需要整页刷新就能在模型下拉里看到新模型。
    const unsubscribe = subscribeModelsChanged(() => { void loadAvailableModels(); });
    return () => {
      stopped = true;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    const requestIDsOnPageLoad = new Set(loadPendingRuns().map((run) => run.requestId));

    async function recoverCompletedRuns() {
      for (const pendingRun of loadPendingRuns()) {
        let statusResponse: Response;
        try {
          statusResponse = await fetch(`/api/agent-runs/${encodeURIComponent(pendingRun.requestId)}`);
        } catch {
          continue;
        }
        if (!statusResponse.ok && statusResponse.status !== 404) {
          continue;
        }

        if (statusResponse.ok) {
          const run = await statusResponse.json() as AgentRunStatus;
          if (run.status !== "running") {
            continue;
          }
          // 后端确认仍在运行：即使本页没有这条流，也必须点亮该会话的运行态，
          // 否则刷新后所有会话都显示空闲，用户会对着一个正在跑的任务再发一条。
          markRunning(pendingRun.sessionId, pendingRun.requestId);
          // 关键：能重连就重连，不能只停在一句「当前页面未连接实时输出」。
          // 后端仍在跑、事件也都还在日志里，订阅回来就能立刻看到已经发生的
          // 执行过程（工具调用、正文增量），而不是等 run 结束才一次性出现。
          if (liveStreamSessionIDs.current.has(pendingRun.sessionId)) {
            continue;
          }
          void resumeRunStreamRef.current(
            pendingRun.sessionId,
            pendingRun.requestId,
            requestIDsOnPageLoad.has(pendingRun.requestId),
          );
          continue;
        }

        let conversationResponse: Response;
        try {
          conversationResponse = await fetch(`/api/conversations/${encodeURIComponent(pendingRun.sessionId)}`);
        } catch {
          continue;
        }
        if (!conversationResponse.ok) {
          if (conversationResponse.status === 404) {
            // Runner 和会话都不存在时，这条 pending 记录已经失效，停止轮询。
            removePendingRun(pendingRun.requestId);
            finishRun(pendingRun.sessionId, pendingRun.requestId);
          }
          continue;
        }
        const conversation = await conversationResponse.json() as ConversationResponse;
        // 会话仍在实时流式输出时，不拿历史覆盖，避免与 SSE 事件竞争丢块。
        if (liveStreamSessionIDs.current.has(pendingRun.sessionId)) {
          continue;
        }
        if (stopped) {
          return false;
        }

        setSessions((current) => current.map((session) =>
          session.id === pendingRun.sessionId
            ? {
                ...session,
                timeline: conversation.timeline,
                usage: conversation.usage,
                taskStatus: conversation.taskStatus ?? null,
              }
            : session,
        ));
        removePendingRun(pendingRun.requestId);
        clearSessionRunNotice(pendingRun.sessionId);
        finishRun(pendingRun.sessionId, pendingRun.requestId);
      }

      // 兜底：以**后端状态**为准，而不是以本标签页的 pending 记录为准。
      //
      // pending 存在 sessionStorage 里，只属于当前标签页；它可能因为换标签页、
      // 清缓存、或者发送时就没写成功而不存在。此时旧逻辑什么也不做，屏幕就停在
      // 「刷新前那样」——只剩用户那一句。这里改成：只要后端说这个会话还有没跑完
      // 的活（task_status=in_progress），又没有本页的实时流，就每 3 秒拉一次历史，
      // 让运行中已经落库的推理/工具调用自己浮现出来。
      for (const session of sessionsRef.current) {
        if (session.taskStatus !== "in_progress") continue;
        if (liveStreamSessionIDs.current.has(session.id)) continue;
        if (stopped) return;
        await alignWithHistory(session.id);
      }
    }

    void recoverCompletedRuns();
    const intervalID = window.setInterval(() => void recoverCompletedRuns(), 3_000);
    window.addEventListener("online", recoverCompletedRuns);
    return () => {
      stopped = true;
      window.clearInterval(intervalID);
      window.removeEventListener("online", recoverCompletedRuns);
    };
  }, []);

  // 拉取完整会话列表（含归档）。返回最新数据，供「工作区是否已有会话」这类判断复用。
  const refreshConversations = useCallback(async (): Promise<ChatSession[]> => {
    const response = await fetch("/api/conversations?includeArchived=true");
    if (!response.ok) return [];
    const responseBody = await response.json() as ConversationListResponse;
    const history = responseBody.conversations.map((conversation) => ({
      ...conversation,
      workspaceId: conversation.workspaceId ?? "",
      archived: conversation.archived ?? false,
      timeline: emptyTimeline,
      usage: emptySessionUsage,
    }));
    // 新用户（或会话被删光）时后端返回空数组。绝不能把 sessions 清空 —— 那样
    // activeSession 会变成 undefined，渲染时读 `activeSession.title` 直接抛错，
    // 表现就是 Next.js 全局错误页「This page couldn't load」且没有 digest。
    if (history.length === 0) {
      setSessions([emptySession]);
      setActiveSessionID(emptySession.id);
      return history;
    }
    // 合并而不是整体替换：列表接口只有「会话有哪些」，没有内容（timeline）。
    // 之前直接 setSessions(history) 会把刚由重连补发重建的时间线清空——
    // 刷新页面时「列表返回」与「事件补发」是两条并发路径，谁后到谁说了算，
    // 一旦列表后到，屏幕就退回到只有一条用户消息的样子（甚至全空）。
    // 已有会话一律保留内容字段，只更新列表提供的元数据。
    setSessions((current) => {
      const byID = new Map(current.map((session) => [session.id, session]));
      return history.map((item) => {
        const existing = byID.get(item.id);
        if (!existing) return item;
        return {
          ...item,
          timeline: existing.timeline,
          usage: existing.usage,
          plan: existing.plan,
          // 列表接口不返回 taskStatus（那是会话详情才有的语义）：保留本页已有的。
          taskStatus: existing.taskStatus,
        };
      });
    });
    return history;
  }, []);

  useEffect(() => {
    async function loadConversations() {
      try {
        const history = await refreshConversations();
        if (history.length === 0) return;
        // 初始会话选第一个未归档的会话
        const firstActive = history.find((session) => !session.archived);
        const requestedSessionID = new URLSearchParams(window.location.search).get("sessionId");
        const initialSessionID = history.some((session) => session.id === requestedSessionID)
          ? requestedSessionID!
          : (firstActive?.id ?? history[0].id);
        void selectSession(initialSessionID);
      } finally {
        setIsLoadingConversations(false);
      }
    }
    loadConversations();
  }, [refreshConversations]);

  // 一个工作区只允许一个会话：点「新会话」时先问后端，已存在就直接打开它。
  // 结构上也不允许绕过（DB 唯一索引），这里只是把冲突提前变成一次跳转。
  async function createSession(workspaceId?: string) {
    if (workspaceId) {
      const existing = await fetchActiveSessionID(workspaceId);
      if (existing) {
        const history = await refreshConversations();
        if (history.some((session) => session.id === existing)) {
          void selectSession(existing);
          return;
        }
      }
    }
    const id = crypto.randomUUID();
    const session: ChatSession = {
      id,
      title: "新对话",
      timeline: { blocks: [] },
      usage: emptySessionUsage,
      workspaceId: workspaceId ?? "",
    };
    // 在工作区内新建会话：激活该工作区
    if (workspaceId) {
      localStorage.setItem("ethos.workspaceId", workspaceId);
      setActiveWorkspaceID(workspaceId);
    }
    setSessions((current) => [session, ...current]);
    setActiveSessionID(id);
    // 记录当前会话 ID：设置页「推送到远端」需要它定位沙箱
    localStorage.setItem("ethos.sessionId", id);
  }

  // 切换工作区 = 切换会话：工作区与会话是一对一的，不能沿用上一个工作区的会话
  // （否则消息会带着新 workspace_id 写进旧会话，镜像目录也就错了）。
  async function switchWorkspace(workspaceId: string | null) {
    if (!workspaceId) {
      setActiveWorkspaceID(null);
      localStorage.removeItem("ethos.workspaceId");
      return;
    }
    setActiveWorkspaceID(workspaceId);
    localStorage.setItem("ethos.workspaceId", workspaceId);
    const existing = await fetchActiveSessionID(workspaceId);
    if (existing) {
      const history = await refreshConversations();
      if (history.some((session) => session.id === existing)) {
        void selectSession(existing);
        return;
      }
    }
    // 该工作区还没有会话（或列表尚未包含它）：直接开一个
    await createSession(workspaceId);
  }

  async function selectSession(sessionID: string) {
    setActiveSessionID(sessionID);
    localStorage.setItem("ethos.sessionId", sessionID);
    // 打开会话时激活其归属工作区（普通对话则清空），保证文件面板与 system prompt 一致
    const target = sessions.find((session) => session.id === sessionID);
    const workspaceId = target?.workspaceId ?? null;
    if (workspaceId) {
      localStorage.setItem("ethos.workspaceId", workspaceId);
    } else {
      localStorage.removeItem("ethos.workspaceId");
    }
    setActiveWorkspaceID(workspaceId);
    if (liveStreamSessionIDs.current.has(sessionID)) {
      return;
    }

    const response = await fetch(`/api/conversations/${encodeURIComponent(sessionID)}`);
    if (!response.ok) return;
    const responseBody = await response.json() as ConversationResponse;
    if (liveStreamSessionIDs.current.has(sessionID)) {
      return;
    }

    setSessions((current) => current.map((session) =>
      session.id === sessionID
        ? {
            ...session,
            timeline: responseBody.timeline,
            usage: responseBody.usage,
            plan: responseBody.plan ?? null,
            taskStatus: responseBody.taskStatus ?? null,
          }
        : session,
    ));
  }

  function selectWorkspace(workspaceId: string) {
    localStorage.setItem("ethos.workspaceId", workspaceId);
    setActiveWorkspaceID(workspaceId);
  }

  // 本次新增：重命名工作区（侧边栏 ··· 菜单触发）
  async function renameWorkspace(workspaceId: string, name: string): Promise<boolean> {
    const response = await fetch(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!response.ok) return false;
    await loadWorkspaces(); // 刷新侧边栏工作区名
    return true;
  }

  // 本次新增：删除工作区（侧边栏 ··· 菜单触发）。若删除的是当前激活工作区则清空激活态。
  async function deleteWorkspaceByID(workspaceId: string): Promise<void> {
    const workspace = workspaces.find((w) => w.workspaceId === workspaceId);
    const name = workspace?.name ?? "该工作区";
    const confirmed = window.confirm(
      `删除工作区「${name}」？\n\n将同时删除该项目指令、自动记忆与云端项目文件，且无法恢复。`,
    );
    if (!confirmed) return;
    const response = await fetch(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      window.alert("删除失败。");
      return;
    }
    if (activeWorkspaceID === workspaceId) {
      localStorage.removeItem("ethos.workspaceId");
      setActiveWorkspaceID(null);
    }
    await loadWorkspaces();
  }

  // 本次新增：归档会话（从侧边栏隐藏）
  async function archiveSession(sessionID: string): Promise<boolean> {
    const response = await fetch(
      `/api/conversations/${encodeURIComponent(sessionID)}/archive`,
      { method: "POST" },
    );
    if (!response.ok) return false;
    // 本地标记归档，UI 立即从列表移除
    setSessions((current) =>
      current.map((session) =>
        session.id === sessionID ? { ...session, archived: true } : session,
      ),
    );
    return true;
  }

  // 本次新增：取消归档（恢复会话到侧边栏）
  async function unarchiveSession(sessionID: string): Promise<boolean> {
    const response = await fetch(
      `/api/conversations/${encodeURIComponent(sessionID)}/unarchive`,
      { method: "POST" },
    );
    if (!response.ok) {
      // 409 = 该工作区已有活跃会话。恢复它会让两个会话共用一份镜像目录，
      // 所以必须是「先归档当前的，再恢复」这个顺序。
      if (response.status === 409) {
        window.alert("该工作区已有会话，请先归档它，再恢复此会话。");
      }
      return false;
    }
    setSessions((current) =>
      current.map((session) =>
        session.id === sessionID ? { ...session, archived: false } : session,
      ),
    );
    return true;
  }

  // 本次新增：删除整个会话（从数据库删除消息+摘要+归档状态）
  async function deleteSession(sessionID: string): Promise<boolean> {
    const response = await fetch(
      `/api/conversations/${encodeURIComponent(sessionID)}`,
      { method: "DELETE" },
    );
    if (!response.ok) return false;
    const remaining = sessions.filter((session) => session.id !== sessionID);
    setSessions(remaining);
    // 若删除的是当前会话，切到第一个未归档会话（若无则回到"新对话"）
    if (activeSessionID === sessionID) {
      const next = remaining.find((session) => !session.archived);
      if (next) {
        setActiveSessionID(next.id);
        if (next.workspaceId) {
          localStorage.setItem("ethos.workspaceId", next.workspaceId);
          setActiveWorkspaceID(next.workspaceId);
        } else {
          localStorage.removeItem("ethos.workspaceId");
          setActiveWorkspaceID(null);
        }
      } else {
        createSession();
      }
    }
    return true;
  }

  function selectModel(nextModelID: string) {
    setModelID(nextModelID);
    setReasoningOptionID("");
  }

  async function cancelRun() {
    // 取消的是「当前会话」的那一个 run，不是全局唯一在跑的那个。
    const sessionID = activeSession.id;
    const requestID = runningBySession[sessionID];
    if (!requestID || cancellingBySession[sessionID]) return;
    abortBySessionRef.current.get(sessionID)?.abort();
    setCancellingBySession((current) => ({ ...current, [sessionID]: true }));
    try {
      const response = await fetch(`/api/agent-runs/${encodeURIComponent(requestID)}`, { method: "POST" });
      if (!response.ok) {
        throw new Error("取消请求未被服务端接受");
      }
    } catch {
      setCancellingBySession((current) => {
        const next = { ...current };
        delete next[sessionID];
        return next;
      });
      setSessionRunNotice(sessionID, "stream_disconnected");
      return; // 服务端是否收到取消请求未知，保留 pending 并继续查询后端状态
    }
    // 204 只表示 Runner 已收到取消信号。继续保留 pending，直到
    // ManagedRunner 不再报告该 requestID 为运行中，才能确认已结束。
  }

  async function sendMessage(content: string, imageIDs: string[], uploadPaths: string[]): Promise<boolean> {
    const sessionID = activeSession.id;
    // 只挡「同一会话重复发」。不同会话本来就该能同时跑：后端按
    // (user_id, session_id) 与 (user_id, workspace_id) 两级互斥，不同会话不冲突。
    if (!modelID || runningBySession[sessionID]) {
      return false;
    }

    // 新任务开始：上一轮残留在时间线底部的横幅必须立刻消失。
    // 不清的话会出现「工具卡片在转、底部却写着任务未完成」的自相矛盾画面——
    // 这条横幅表达的是上一轮的状态，和当前这一轮没有任何关系。
    clearSessionRunNotice(sessionID);
    const requestID = crypto.randomUUID();
    markRunning(sessionID, requestID);
    savePendingRun({ requestId: requestID, sessionId: sessionID });
    const now = new Date().toISOString();
    const userBlock: UserBlock = {
      type: "user",
      id: crypto.randomUUID(),
      content: messageWithUploads(content, uploadPaths),
      images: imageIDs.map((id) => ({ id })),
      createdAt: now,
    };
    setSessions((current) =>
      current.map((session) => {
        if (session.id !== sessionID) {
          return session;
        }

        return {
          ...session,
          title: session.timeline.blocks.length === 0 ? (content.slice(0, 18) || (uploadPaths.length > 0 ? "附件" : "图片")) : session.title,
          timeline: {
            blocks: [...session.timeline.blocks, userBlock],
          },
        };
      }),
    );
    liveStreamSessionIDs.current.add(sessionID);
    const abort = new AbortController();
    abortBySessionRef.current.set(sessionID, abort);

    try {
      const response = await fetch("/api/chat/stream", {
        signal: abort.signal,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requestId: requestID,
          sessionId: sessionID,
          message: content,
          imageIds: imageIDs,
          uploadPaths,
          modelId: modelID,
          ...(reasoningOptionID ? { reasoningOptionId: reasoningOptionID } : {}),
          ...(activeSession.workspaceId ? { workspaceId: activeSession.workspaceId } : {}),
        }),
      });
      if (!response.ok) {
        if (response.status === 409) {
          // 该会话已有任务在运行，或该工作区正被另一个会话占用：这条消息没有被接受。
          // 保留刚插入的用户消息块，只显示提示，避免用户以为自己的消息丢了。
          // 409 有两种语义，读后端给的 type 决定文案：猜错会让用户去等一个
          // 永远不会结束的「同一会话任务」。
          const busy = await response.json().catch(() => null) as { type?: string } | null;
          removePendingRun(requestID);
          setSessionRunNotice(
            sessionID,
            // 409 有三种语义，文案不能合并：猜错会让用户去等一个已经结束的任务，
            // 或者在一个没有冲突的会话里反复重试。
            busy?.type === "workspace_busy"
              ? "workspace_busy"
              : busy?.type === "request_conflict"
                ? "duplicate_request"
                : "session_busy",
          );
          liveStreamSessionIDs.current.delete(sessionID);
          finishRun(sessionID, requestID);
          return false;
        }

        removePendingRun(requestID);
        liveStreamSessionIDs.current.delete(sessionID);
        finishRun(sessionID, requestID);
        const errorMessage = await response.text();
        setSessions((current) => current.map((session) =>
          session.id === sessionID
            ? { ...session, timeline: { blocks: [...session.timeline.blocks, errorTimelineEvent(errorMessage)] } }
            : session,
        ));
        return false;
      }

      void consumeRunStream(sessionID, requestID, response);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        liveStreamSessionIDs.current.delete(sessionID);
        return false; // 用户主动取消，不发错误提示
      }
      liveStreamSessionIDs.current.delete(sessionID);
      if (!abort.signal.aborted) {
        // 只清运行态、保留 pending：请求失败时 run 可能仍在后台跑，轮询会继续查。
        finishRun(sessionID, requestID);
      }
      setSessionRunNotice(sessionID, "stream_disconnected");
      return false;
    }
  }

  return (
    <main className="flex h-screen overflow-hidden bg-canvas">
      <AppSidebar footer={<AccountMenu onOpenSettings={() => setSettingsOpen(true)} />}>
        <ConversationList
          activeSessionID={activeSession.id}
          isLoading={isLoadingConversations}
          sessions={sessions}
          runningSessionIDs={runningSessionIDs}
          workspaces={workspaces}
          activeWorkspaceID={activeWorkspaceID}
          onCreate={createSession}
          onSelect={selectSession}
          onSelectWorkspace={selectWorkspace}
          onAddWorkspace={() => workspaceSwitcherRef.current?.triggerFilePicker()}
          onRenameWorkspace={renameWorkspace}
          onDeleteWorkspace={deleteWorkspaceByID}
          onArchiveSession={archiveSession}
          onUnarchiveSession={unarchiveSession}
          onDeleteSession={deleteSession}
        />
      </AppSidebar>

      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-surface px-5">
          <div>
            <div className="flex items-center gap-2">
              <p className="text-[13px] font-semibold text-ink">{activeSession.title}</p>
              {activeRunning && (
                <span
                  aria-label="执行中"
                  className="size-3 animate-spin rounded-full border-[1.5px] border-accent/25 border-t-accent"
                  title="本会话正在执行"
                />
              )}
            </div>
            <p className="mt-0.5 text-[11px] font-medium tracking-wide text-faint">ETHOS</p>
          </div>
          <div className="flex items-center gap-1">
            {/* 并发提示：让「别的会话还在跑」在任意页面上都可见，点一下切过去。
                侧边栏折叠后行内标识会整块消失，这个胶囊是那时唯一的入口。 */}
            {otherRunningCount > 0 && (
              <button
                className="mr-1 flex h-7 items-center gap-1.5 rounded-full border border-border bg-accent-soft px-2.5 text-[11px] font-medium text-accent transition-colors hover:border-accent/40"
                onClick={() => {
                  const next = runningSessionIDs.find((id) => id !== activeSession.id);
                  if (next) void selectSession(next);
                }}
                title="点击切换到另一个正在执行的会话"
                type="button"
              >
                <span className="size-1.5 animate-pulse rounded-full bg-accent" />
                另有 {otherRunningCount} 个任务执行中
              </button>
            )}
            <SessionUsageView menuPosition="down" usage={activeSession.usage} />
            <button
              className="ui-icon-button"
              onClick={() => setSandboxOpen((current) => !current)}
              title={sandboxOpen ? "关闭 Sandbox 文件" : "展开 Sandbox 文件"}
              aria-label={sandboxOpen ? "关闭 Sandbox 文件" : "展开 Sandbox 文件"}
            >
              {sandboxOpen ? <PanelRightClose className="size-5" /> : <PanelRightOpen className="size-5" />}
            </button>
          </div>
        </header>

        <TimelineView
          pendingTask={pendingTask}
          plan={activeSession.plan}
          planRunning={activeRunning}
          runNotice={sessionRunNotices[activeSession.id]}
          timeline={activeSession.timeline}
        />

        <ChatComposer
          key={activeSession.id}
          isLoading={isLoadingConversations}
          isRunning={activeRunning}
          isCancelling={isCancelling}
          archived={!!activeSession.archived}
          sessionID={activeSession.id}
          modelID={modelID}
          models={models}
          reasoningOptionID={reasoningOptionID}
          selectedModel={selectedModel}
          onCancel={cancelRun}
          onModelChange={selectModel}
          onReasoningOptionChange={setReasoningOptionID}
          onSend={sendMessage}
          topLeft={
            <WorkspaceSwitcher
              ref={workspaceSwitcherRef}
              workspaceId={activeWorkspaceID}
              onSelect={(workspaceId) => { void switchWorkspace(workspaceId); }}
              onWorkspacesChanged={loadWorkspaces}
            />
          }
        />
      </section>

      <SandboxPanel
        key={activeSession.id}
        sessionID={activeSession.id}
        workspaceId={activeWorkspaceID}
        open={sandboxOpen}
      />

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </main>
  );
}
