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
const pendingRunsKey = "edith.pending-agent-runs";

type PendingRun = {
  requestId: string;
  sessionId: string;
};

type SessionRunNotice = {
  sessionID: string;
  reason: TimelineRunNotice;
};

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

function messageWithUploads(content: string, uploadPaths: string[]) {
  const message = content.trim();
  if (uploadPaths.length === 0) return message;
  const files = uploadPaths.map((path) => `- \`${path.split("/").at(-1) || path}\``).join("\n");
  const attachments = `附件：\n${files}`;
  return message ? `${message}\n\n${attachments}` : attachments;
}

export function ChatPage() {
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [activeRequestID, setActiveRequestID] = useState<string | null>(null);
  const [isLoadingConversations, setIsLoadingConversations] = useState(true);
  const [sessions, setSessions] = useState<ChatSession[]>([
    { id: "new-session", title: "新对话", timeline: emptyTimeline, usage: emptySessionUsage },
  ]);
  const [activeSessionID, setActiveSessionID] = useState("new-session");
  const [activeWorkspaceID, setActiveWorkspaceID] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelID, setModelID] = useState("");
  const [reasoningOptionID, setReasoningOptionID] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sandboxOpen, setSandboxOpen] = useState(false);
  const [sessionRunNotice, setSessionRunNotice] = useState<SessionRunNotice | null>(null);
  const liveStreamSessionIDs = useRef(new Set<string>());
  const abortRef = useRef<AbortController | null>(null);
  const activeRequestIDRef = useRef<string | null>(null);
  const workspaceSwitcherRef = useRef<WorkspaceSwitcherHandle>(null); // 本次新增：侧边栏"+ 添加工作区"按钮复用 WorkspaceSwitcher 的文件选择

  const activeSession = sessions.find((session) => session.id === activeSessionID) ?? sessions[0];
  const selectedModel = models.find((model) => model.id === modelID);

  // 从 localStorage 恢复当前工作区 ID（顶部切换器写入）；没有则工作区指令不注入。
  useEffect(() => {
    setActiveWorkspaceID(localStorage.getItem("ethos.workspaceId") ?? null);
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

  function finishActiveRun(requestID: string) {
    if (activeRequestIDRef.current !== requestID) return;
    activeRequestIDRef.current = null;
    setActiveRequestID(null);
    setIsRunning(false);
    setIsCancelling(false);
  }

  function clearSessionRunNotice(sessionID: string) {
    setSessionRunNotice((current) => current?.sessionID === sessionID ? null : current);
  }

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
          if (requestIDsOnPageLoad.has(pendingRun.requestId)) {
            setSessionRunNotice({ sessionID: pendingRun.sessionId, reason: "stream_disconnected" });
          }
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
            finishActiveRun(pendingRun.requestId);
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
            ? { ...session, timeline: conversation.timeline, usage: conversation.usage }
            : session,
        ));
        removePendingRun(pendingRun.requestId);
        clearSessionRunNotice(pendingRun.sessionId);
        finishActiveRun(pendingRun.requestId);
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
    setSessions(history);
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
      session.id === sessionID ? { ...session, timeline: responseBody.timeline, usage: responseBody.usage } : session,
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
    if (!activeRequestID || isCancelling) return;
    abortRef.current?.abort();
    setIsCancelling(true);
    try {
      const response = await fetch(`/api/agent-runs/${encodeURIComponent(activeRequestID)}`, { method: "POST" });
      if (!response.ok) {
        throw new Error("取消请求未被服务端接受");
      }
    } catch {
      setIsCancelling(false);
      setSessionRunNotice({ sessionID: activeSessionID, reason: "stream_disconnected" });
      return; // 服务端是否收到取消请求未知，保留 pending 并继续查询后端状态
    }
    // 204 只表示 Runner 已收到取消信号。继续保留 pending，直到
    // ManagedRunner 不再报告该 requestID 为运行中，才能确认已结束。
  }

  async function sendMessage(content: string, imageIDs: string[], uploadPaths: string[]): Promise<boolean> {
    if (!modelID || isRunning) {
      return false;
    }

    const sessionID = activeSession.id;
    const requestID = crypto.randomUUID();
    activeRequestIDRef.current = requestID;
    setActiveRequestID(requestID);
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
    setIsRunning(true);
    liveStreamSessionIDs.current.add(sessionID);
    const abort = new AbortController();
    abortRef.current = abort;

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
          // 该会话已有任务在运行，这条消息没有被接受。
          // 保留刚插入的用户消息块，只显示“会话忙”提示，避免用户以为自己的消息丢了。
          removePendingRun(requestID);
          setSessionRunNotice({ sessionID, reason: "session_busy" });
          liveStreamSessionIDs.current.delete(sessionID);
          finishActiveRun(requestID);
          return false;
        }

        removePendingRun(requestID);
        liveStreamSessionIDs.current.delete(sessionID);
        finishActiveRun(requestID);
        const errorMessage = await response.text();
        setSessions((current) => current.map((session) =>
          session.id === sessionID
            ? { ...session, timeline: { blocks: [...session.timeline.blocks, errorTimelineEvent(errorMessage)] } }
            : session,
        ));
        return false;
      }

      void consumeStream(response);
      return true;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        liveStreamSessionIDs.current.delete(sessionID);
        return false; // 用户主动取消，不发错误提示
      }
      liveStreamSessionIDs.current.delete(sessionID);
      if (!abort.signal.aborted) {
        setIsRunning(false);
      }
      setSessionRunNotice({ sessionID, reason: "stream_disconnected" });
      return false;
    }

    async function consumeStream(response: Response) {
      let streamCompleted = false;
      try {
        await readChatStream(response, (event) => {
          if (event.type === "run.completed" || event.type === "run.canceled") {
            streamCompleted = true;
            removePendingRun(requestID);
            finishActiveRun(requestID);
            // 轮次耗尽未完成：明确提示（与"正常完成"区分），引导用户继续
            if (event.type === "run.completed" && event.endReason === "rounds_exhausted") {
              setSessionRunNotice({ sessionID, reason: "rounds_exhausted" });
            }
          }
          setSessions((current) => current.map((session) =>
            session.id === sessionID
              ? {
                  ...session,
                  timeline: applyStreamEvent(session.timeline, event),
                  usage: (event.type === "run.completed" || event.type === "run.canceled") && event.sessionUsage ? event.sessionUsage : session.usage,
                }
              : session,
          ));
        });
        if (!streamCompleted) {
          throw new Error("网络连接已断开，任务仍在后台继续。");
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setSessionRunNotice({ sessionID, reason: "stream_disconnected" });
        }
      } finally {
        liveStreamSessionIDs.current.delete(sessionID);
        if (!abort.signal.aborted) {
          setIsRunning(false);
        }
      }
    }
  }

  return (
    <main className="flex h-screen overflow-hidden bg-canvas">
      <AppSidebar footer={<AccountMenu onOpenSettings={() => setSettingsOpen(true)} />}>
        <ConversationList
          activeSessionID={activeSession.id}
          isLoading={isLoadingConversations}
          sessions={sessions}
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
            <p className="text-[13px] font-semibold text-ink">{activeSession.title}</p>
            <p className="mt-0.5 text-[11px] font-medium tracking-wide text-faint">ETHOS</p>
          </div>
          <div className="flex items-center gap-1">
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
          runNotice={sessionRunNotice?.sessionID === activeSession.id ? sessionRunNotice.reason : undefined}
          timeline={activeSession.timeline}
        />

        <ChatComposer
          key={activeSession.id}
          isLoading={isLoadingConversations}
          isRunning={isRunning}
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
