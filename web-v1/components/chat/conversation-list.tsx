import Link from "next/link";
import { Archive, ArchiveRestore, Blocks, ChevronDown, ChevronRight, Clock3, Folder, FolderPlus, MessageSquare, MessageSquarePlus, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

type ConversationItem = {
  id: string;
  title: string;
  workspaceId?: string; // 空 = 普通对话
  archived?: boolean; // 本次新增：是否已归档
};

type WorkspaceItem = {
  workspaceId: string;
  name: string;
};

type ConversationListProps = {
  activeSessionID: string;
  activePage?: "chat" | "tasks" | "extensions";
  isLoading: boolean;
  sessions: ConversationItem[];
  workspaces?: WorkspaceItem[];
  activeWorkspaceID?: string | null;
  onCreate: (workspaceId?: string) => void;
  onSelect: (sessionID: string) => void;
  onSelectWorkspace?: (workspaceId: string) => void;
  onAddWorkspace?: () => void;
  onRenameWorkspace?: (workspaceId: string, name: string) => Promise<boolean>;
  onDeleteWorkspace?: (workspaceId: string) => void;
  onArchiveSession?: (sessionID: string) => Promise<boolean>;
  onUnarchiveSession?: (sessionID: string) => Promise<boolean>;
  onDeleteSession?: (sessionID: string) => Promise<boolean>;
};

// 侧边栏会话列表：顶部「新对话/定时任务/扩展」+「工作区」分组（每个工作区下显示其会话）
// +「最近对话」（仅未归属工作区的普通对话）。工作区分组默认折叠，点击展开。
export function ConversationList({
  activeSessionID,
  activePage = "chat",
  isLoading,
  sessions,
  workspaces = [],
  activeWorkspaceID = null,
  onCreate,
  onSelect,
  onSelectWorkspace,
  onAddWorkspace,
  onRenameWorkspace,
  onDeleteWorkspace,
  onArchiveSession,
  onUnarchiveSession,
  onDeleteSession,
}: ConversationListProps) {
  // 默认展开当前激活工作区，其余折叠
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<Set<string>>(
    () => new Set(activeWorkspaceID ? [activeWorkspaceID] : []),
  );
  // 本次新增：工作区 ··· 菜单（重命名/删除）+ 重命名弹窗
  const [openMenuWorkspaceID, setOpenMenuWorkspaceID] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<{ workspaceId: string; name: string } | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  // 本次新增：会话 ··· 菜单（归档/取消归档）+ 归档折叠区
  const [openSessionMenuID, setOpenSessionMenuID] = useState<string | null>(null);
  const [archivedOpen, setArchivedOpen] = useState(false);

  function toggleWorkspace(workspaceId: string) {
    setExpandedWorkspaces((current) => {
      const next = new Set(current);
      if (next.has(workspaceId)) {
        next.delete(workspaceId);
      } else {
        next.add(workspaceId);
      }
      return next;
    });
  }

  function startRename(workspaceId: string, currentName: string) {
    setOpenMenuWorkspaceID(null);
    setRenameTarget({ workspaceId, name: currentName });
    setRenameDraft(currentName);
  }

  async function submitRename() {
    if (!renameTarget || !onRenameWorkspace) return;
    const name = renameDraft.trim();
    if (!name || name === renameTarget.name) {
      setRenameTarget(null);
      return;
    }
    setRenameBusy(true);
    const ok = await onRenameWorkspace(renameTarget.workspaceId, name);
    setRenameBusy(false);
    if (ok) {
      setRenameTarget(null);
    } else {
      window.alert("重命名失败。");
    }
  }

  // 分组：先分归档，再按 workspaceId 归属
  const byWorkspace = new Map<string, ConversationItem[]>();
  const normal: ConversationItem[] = [];
  const archived: ConversationItem[] = [];
  for (const session of sessions) {
    if (session.archived) {
      archived.push(session);
    } else if (session.workspaceId) {
      const list = byWorkspace.get(session.workspaceId) ?? [];
      list.push(session);
      byWorkspace.set(session.workspaceId, list);
    } else {
      normal.push(session);
    }
  }

  const sessionRow = (session: ConversationItem) => {
    const isMenuOpen = openSessionMenuID === session.id;
    const hasActions =
      (session.archived ? onUnarchiveSession || onDeleteSession : onArchiveSession) != null;
    return (
      <div key={session.id} className="group relative">
        <button
          className={`w-full truncate rounded-lg px-2.5 py-1.5 text-left text-sm leading-5 transition-colors ${
            session.id === activeSessionID
              ? "bg-accent-soft font-medium text-accent"
              : "text-muted hover:bg-surface-subtle hover:text-ink"
          }`}
          onClick={() => onSelect(session.id)}
        >
          {session.title}
        </button>
        {hasActions && (
          <button
            className={`absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md transition-opacity ${
              isMenuOpen
                ? "bg-surface-subtle text-ink opacity-100"
                : "text-faint opacity-0 hover:bg-surface-subtle hover:text-ink group-hover:opacity-100"
            }`}
            title="会话操作"
            aria-label="会话操作"
            onClick={(e) => {
              e.stopPropagation();
              setOpenSessionMenuID(isMenuOpen ? null : session.id);
            }}
          >
            <MoreHorizontal className="size-3.5" />
          </button>
        )}
        {isMenuOpen && (
          <div className="absolute right-0 top-7 z-20 min-w-28 rounded-lg border border-border bg-surface p-1 shadow-lg">
            {session.archived ? (
              <>
                <button
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-ink transition-colors hover:bg-surface-subtle"
                  onClick={() => {
                    setOpenSessionMenuID(null);
                    void onUnarchiveSession?.(session.id);
                  }}
                >
                  <ArchiveRestore className="size-3.5" />取消归档
                </button>
                {onDeleteSession && (
                  <button
                    className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-danger transition-colors hover:bg-surface-subtle"
                    onClick={() => {
                      setOpenSessionMenuID(null);
                      void onDeleteSession(session.id);
                    }}
                  >
                    <Trash2 className="size-3.5" />删除
                  </button>
                )}
              </>
            ) : (
              <button
                className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-ink transition-colors hover:bg-surface-subtle"
                onClick={() => {
                  setOpenSessionMenuID(null);
                  void onArchiveSession?.(session.id);
                }}
              >
                <Archive className="size-3.5" />归档
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col p-3">
      <div className="shrink-0">
        <button
          className="mb-1 flex h-8 items-center gap-2 rounded-lg px-2.5 text-left text-sm font-medium text-muted transition-colors hover:bg-surface-subtle hover:text-ink disabled:cursor-wait disabled:opacity-60"
          disabled={isLoading}
          onClick={() => onCreate()}
        >
          <MessageSquarePlus className="size-4" />新对话
        </button>
        <Link
          className={`mb-1 flex h-8 items-center gap-2 rounded-lg px-2.5 text-sm font-medium transition-colors ${
            activePage === "tasks"
              ? "bg-accent-soft text-accent"
              : "text-muted hover:bg-surface-subtle hover:text-ink"
          }`}
          href="/tasks"
        >
          <Clock3 className="size-4" />定时任务
        </Link>
        <Link
          className={`mb-4 flex h-8 items-center gap-2 rounded-lg px-2.5 text-sm font-medium transition-colors ${
            activePage === "extensions"
              ? "bg-accent-soft text-accent"
              : "text-muted hover:bg-surface-subtle hover:text-ink"
          }`}
          href="/extensions"
        >
          <Blocks className="size-4" />扩展
        </Link>
      </div>

      <nav aria-label="工作区与会话" className="min-h-0 flex-1 overflow-y-auto border-t border-border pt-3">
        {/* 工作区分组 */}
        {workspaces.length > 0 && (
          <div className="mb-3">
            <div className="mb-2 flex items-center justify-between px-2">
              <p className="flex items-center gap-2 text-sm font-medium tracking-[0.04em] text-faint">
                <Folder className="size-3.5" />工作区
              </p>
              {onAddWorkspace && (
                <button
                  className="flex h-5 w-5 items-center justify-center rounded text-faint transition-colors hover:bg-surface-subtle hover:text-ink"
                  title="添加工作区"
                  aria-label="添加工作区"
                  onClick={onAddWorkspace}
                >
                  <FolderPlus className="size-3.5" />
                </button>
              )}
            </div>
            <div className="space-y-1">
              {workspaces.map((workspace) => {
                const isExpanded = expandedWorkspaces.has(workspace.workspaceId);
                const isActive = workspace.workspaceId === activeWorkspaceID;
                const workspaceSessions = byWorkspace.get(workspace.workspaceId) ?? [];
                const isMenuOpen = openMenuWorkspaceID === workspace.workspaceId;
                const showActions = (onRenameWorkspace || onDeleteWorkspace) && isExpanded;
                return (
                  <div key={workspace.workspaceId} className="relative">
                    <div className="flex items-center gap-0.5">
                      <button
                        className={`flex min-w-0 flex-1 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
                          isActive
                            ? "font-medium text-ink"
                            : "text-muted hover:bg-surface-subtle hover:text-ink"
                        }`}
                        onClick={() => {
                          onSelectWorkspace?.(workspace.workspaceId);
                          toggleWorkspace(workspace.workspaceId);
                        }}
                        title={`工作区：${workspace.name}`}
                      >
                        {isExpanded ? (
                          <ChevronDown className="size-3.5 shrink-0 text-faint" />
                        ) : (
                          <ChevronRight className="size-3.5 shrink-0 text-faint" />
                        )}
                        <Folder className="size-4 shrink-0 text-warning" />
                        <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                      </button>
                      {showActions && (
                        <button
                          className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors ${
                            isMenuOpen ? "bg-surface-subtle text-ink" : "text-faint hover:bg-surface-subtle hover:text-ink"
                          }`}
                          title="工作区操作"
                          aria-label="工作区操作"
                          onClick={(e) => {
                            e.stopPropagation();
                            setOpenMenuWorkspaceID(isMenuOpen ? null : workspace.workspaceId);
                          }}
                        >
                          <MoreHorizontal className="size-4" />
                        </button>
                      )}
                    </div>
                    {isMenuOpen && (onRenameWorkspace || onDeleteWorkspace) && (
                      <div className="absolute right-0 top-8 z-20 min-w-32 rounded-lg border border-border bg-surface p-1 shadow-lg">
                        {onRenameWorkspace && (
                          <button
                            className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-ink transition-colors hover:bg-surface-subtle"
                            onClick={() => startRename(workspace.workspaceId, workspace.name)}
                          >
                            <Pencil className="size-3.5" />重命名
                          </button>
                        )}
                        {onDeleteWorkspace && (
                          <button
                            className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs text-danger transition-colors hover:bg-surface-subtle"
                            onClick={() => {
                              setOpenMenuWorkspaceID(null);
                              onDeleteWorkspace(workspace.workspaceId);
                            }}
                          >
                            <Trash2 className="size-3.5" />删除工作区
                          </button>
                        )}
                      </div>
                    )}
                    {isExpanded && (
                      <div className="ml-5 space-y-0.5 border-l border-border pl-2">
                        {/* 一个工作区只允许一个会话：已有会话时不再提供「新会话」入口，
                            只显示那一条。要重开就先归档它（会话菜单里）。 */}
                        {workspaceSessions.length === 0 && (
                          <button
                            className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-xs text-muted transition-colors hover:bg-surface-subtle hover:text-ink"
                            onClick={() => onCreate(workspace.workspaceId)}
                          >
                            <MessageSquarePlus className="size-3.5" />新会话
                          </button>
                        )}
                        {workspaceSessions.map(sessionRow)}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* 最近对话（仅普通对话） */}
        <p className="mb-2 flex items-center gap-2 px-2 text-sm font-medium tracking-[0.04em] text-faint">
          <MessageSquare className="size-3.5" />最近对话
        </p>
        <div className="space-y-1">
          {normal.length === 0 ? (
            <p className="px-2.5 py-2 text-xs leading-5 text-faint">暂无普通对话。</p>
          ) : (
            normal.map(sessionRow)
          )}
        </div>

        {/* 本次新增：归档折叠区（展示已归档会话，可取消归档） */}
        {archived.length > 0 && (
          <div className="mt-3">
            <button
              className="mb-1 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm font-medium tracking-[0.04em] text-faint transition-colors hover:bg-surface-subtle hover:text-ink"
              onClick={() => setArchivedOpen((current) => !current)}
            >
              {archivedOpen ? (
                <ChevronDown className="size-3.5" />
              ) : (
                <ChevronRight className="size-3.5" />
              )}
              <Archive className="size-3.5" />已归档
              <span className="text-xs text-faint">({archived.length})</span>
            </button>
            {archivedOpen && (
              <div className="space-y-1">{archived.map(sessionRow)}</div>
            )}
          </div>
        )}
      </nav>

      {/* 本次新增：重命名工作区弹窗 */}
      {renameTarget && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/40"
          onClick={() => !renameBusy && setRenameTarget(null)}
        >
          <div
            className="w-80 rounded-xl border border-border bg-surface p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="text-sm font-medium text-ink">重命名工作区</p>
            <input
              className="ui-field mt-3 h-8 w-full px-2.5 text-sm"
              autoFocus
              value={renameDraft}
              disabled={renameBusy}
              onChange={(event) => setRenameDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void submitRename();
                } else if (event.key === "Escape") {
                  event.preventDefault();
                  setRenameTarget(null);
                }
              }}
            />
            <div className="mt-3 flex justify-end gap-2">
              <button
                className="ui-button-secondary h-7 px-3 text-xs"
                type="button"
                disabled={renameBusy}
                onClick={() => setRenameTarget(null)}
              >
                取消
              </button>
              <button
                className="ui-button-primary h-7 px-3 text-xs"
                type="button"
                disabled={renameBusy || !renameDraft.trim() || renameDraft.trim() === renameTarget.name}
                onClick={() => void submitRename()}
              >
                {renameBusy ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
