"use client";

import { useAuth } from "@clerk/nextjs";
import { Check, ChevronDown, Folder, FolderPlus, LoaderCircle, Trash2 } from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";

import { uploadWorkspaceFiles, filterWorkspaceFiles, type WorkspaceUploadFile } from "@/lib/sandbox/files";

const STORAGE_KEY = "ethos.workspaceId";

// 从本地绝对路径取末级目录名（用于绑定本地目录时自动填充工作区名称）
function localDirName(path: string): string {
  return (
    path
      .trim()
      .replace(/[\\/]+$/, "")
      .split(/[\\/]/)
      .pop() ?? ""
  );
}

type Workspace = {
  workspaceId: string;
  name: string;
  metadata?: {
    folderName?: string;
    fileCount?: number;
  };
  // 方案 B：绑定的本地绝对目录（空 = 未绑定，走云端副本）
  localRoot?: string;
};

type Props = {
  workspaceId: string | null;
  onSelect: (workspaceId: string | null) => void;
  onWorkspacesChanged?: () => void; // 创建/删除工作区后通知上层刷新侧边栏列表
};

type CreateState = {
  name: string;
  files: File[];
  workspaceId?: string; // 已创建但上传失败时保留，供重试
  // 方案 B：创建方式。upload = 选择文件夹上传快照；bind = 绑定本地绝对目录（后端直读直写）
  mode?: "upload" | "bind";
  localRoot?: string;
  // 绑定已有工作区时：是否把旧副本（未绑定期间的产物）合并过来。默认关，避免整棵搬平。
  mergeExisting?: boolean;
  // 绑定已有工作区时填其 id（此时不需要名称/上传，只补 localRoot）
  targetWorkspaceId?: string;
};

// 本次新增：暴露 handle 让 chat-page 触发文件选择（侧边栏"+ 添加工作区"按钮复用此能力）
export type WorkspaceSwitcherHandle = {
  triggerFilePicker: () => void;
};

// 聊天页左下角对话框上方的工作区切换器：
// 选择/新建（选文件夹 → 自动命名 → 真实上传到 Volume）/删除（二次确认，级联清指令）。
export const WorkspaceSwitcher = forwardRef<WorkspaceSwitcherHandle, Props>(function WorkspaceSwitcher(
  { workspaceId, onSelect, onWorkspacesChanged },
  ref,
) {
  const { userId } = useAuth(); // 方案 C：直传后端需要 userId（原 BFF 从 Clerk 注入）
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [open, setOpen] = useState(false);
  const [create, setCreate] = useState<CreateState | null>(null);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    async function load() {
      const response = await fetch("/api/workspaces");
      if (!response.ok) return;
      const value = (await response.json()) as { workspaces: Workspace[] };
      setWorkspaces(value.workspaces);
    }
    void load();
  }, [workspaceId]);

  useEffect(() => {
    function onDocClick(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  const active = workspaces.find((w) => w.workspaceId === workspaceId) ?? null;

  function triggerFilePicker() {
    setMessage("");
    inputRef.current?.click();
  }

  // 本次新增：暴露 triggerFilePicker 给父组件（侧边栏"+ 添加工作区"按钮复用此能力）
  useImperativeHandle(ref, () => ({ triggerFilePicker }), []);

  function onFilesPicked(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    // 本次新增（2026-09-01）：诊断日志——确认浏览器 FileList 是否被枚举截断（曾出现选 3 万文件只收到 100 个）
    console.log(
      "[workspace-upload] picked files:",
      files.length,
      "first:",
      files[0]?.webkitRelativePath ?? "(none)",
    );
    event.target.value = ""; // 允许重复选择同一文件夹
    if (files.length === 0) return;
    // webkitRelativePath 形如 "har_agents/src/main.py" → 顶层目录即文件夹名
    const folderName = files[0].webkitRelativePath?.split("/")[0] ?? "";
    if (!folderName) {
      setMessage("请选择文件夹（当前浏览器可能不支持文件夹上传）。");
      return;
    }
    setCreate({ name: folderName, files, mode: "upload" });
  }

  // 方案 B：打开「绑定本地目录」表单——不选文件夹，直接填本地绝对路径
  function startBindLocal() {
    setMessage("");
    setOpen(false);
    setCreate({ name: "", files: [], mode: "bind", localRoot: "" });
  }

  // 方案 B：把「当前已有工作区」绑定到本地目录（无需重建工作区）
  // 典型场景：工作区是早前"选文件夹上传"创建的，卷里多一层 Data/，后端会自动探测前缀
  function startBindCurrent() {
    if (!workspaceId) return;
    const current = workspaces.find((w) => w.workspaceId === workspaceId);
    if (!current) return;
    setMessage("");
    setOpen(false);
    setCreate({
      name: current.name,
      files: [],
      mode: "bind",
      localRoot: "",
      targetWorkspaceId: workspaceId,
    });
  }

  async function createWorkspace() {
    if (!create || uploading) return;
    const name = create.name.trim();

    // ===== 方案 B：绑定本地目录（不上传文件，后端直接以该目录为存储根）=====
    if (create.mode === "bind") {
      const localRoot = (create.localRoot ?? "").trim();
      if (!localRoot) {
        setMessage("请填写本地目录的绝对路径。");
        return;
      }
      const targetId = create.targetWorkspaceId;
      if (!targetId && !name) {
        setMessage("请填写工作区名称。");
        return;
      }
      setUploading(true);
      setMessage("");
      try {
        if (targetId) {
          // 已有工作区：只补绑定，不新建
          const response = await fetch(
            `/api/workspaces/${encodeURIComponent(targetId)}/bind-local-root`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                localRoot,
                mergeExisting: create.mergeExisting === true,
              }),
            },
          );
          if (!response.ok) {
            const body = (await response.json().catch(() => null)) as { message?: string } | null;
            setMessage(body?.message ?? "绑定失败：请检查路径是否存在、是否在白名单内。");
            return;
          }
          const result = (await response.json()) as {
            workspaceId: string;
            localRoot: string;
            localRootPrefix?: string;
            mergedFiles?: number;
            unmergedFiles?: number;
          };
          setWorkspaces((current) =>
            current.map((w) =>
              w.workspaceId === targetId ? { ...w, localRoot: result.localRoot } : w,
            ),
          );
          setCreate(null);
          setOpen(false);
          setMessage(
            [
              `已绑定：${result.localRoot}`,
              result.localRootPrefix ? `映射 ${result.localRootPrefix}/` : "",
              result.mergedFiles ? `已合并 ${result.mergedFiles} 个旧文件` : "",
              result.unmergedFiles
                ? `另有 ${result.unmergedFiles} 个旧文件未合并（如需要，可再次绑定并勾选「合并存量文件」）`
                : "",
            ]
              .filter(Boolean)
              .join("，"),
          );
          onWorkspacesChanged?.();
          return;
        }
        // 新建工作区并绑定目录
        const response = await fetch("/api/workspaces", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, localRoot, metadata: { folderName: name } }),
        });
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as { message?: string } | null;
          setMessage(body?.message ?? "绑定失败：请检查路径是否存在、是否在白名单内。");
          return;
        }
        const workspace = (await response.json()) as Workspace;
        setWorkspaces((current) => [...current, workspace]);
        setCreate(null);
        setOpen(false);
        localStorage.setItem(STORAGE_KEY, workspace.workspaceId);
        onSelect(workspace.workspaceId);
        onWorkspacesChanged?.();
      } finally {
        setUploading(false);
      }
      return;
    }

    if (!name) return;

    // 1. 创建空工作区（metadata 只记文件夹名，文件数由后端上传后更新）
    let workspaceId = create.workspaceId;
    if (!workspaceId) {
      const response = await fetch("/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, metadata: { folderName: name } }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { message?: string } | null;
        setMessage(body?.message ?? "创建工作区失败。");
        return;
      }
      const workspace = (await response.json()) as Workspace;
      workspaceId = workspace.workspaceId;
      setWorkspaces((current) => [...current, workspace]);
    }

    // 2. 真实上传文件到 Volume（P1）
    setUploading(true);
    setMessage("");
    try {
      const items: WorkspaceUploadFile[] = create.files.map((file) => ({
        relPath: file.webkitRelativePath ?? file.name,
        blob: file,
      }));
      // 本次新增：诊断日志——过滤前后数量对比，确认浏览器 FileList 是否被枚举截断
      console.log(
        "[workspace-upload] total files:",
        items.length,
        "after filter:",
        filterWorkspaceFiles(items).length,
      );
      if (!userId) {
        setMessage("无法获取当前用户身份，请重新登录。");
        return;
      }
      const result = await uploadWorkspaceFiles(workspaceId, items, userId);
      setWorkspaces((current) =>
        current.map((w) =>
          w.workspaceId === workspaceId
            ? { ...w, metadata: { folderName: name, fileCount: result.fileCount } }
            : w,
        ),
      );
      setCreate(null);
      setOpen(false);
      localStorage.setItem(STORAGE_KEY, workspaceId);
      onSelect(workspaceId);
      onWorkspacesChanged?.();
    } catch (error) {
      // 上传失败：保留空工作区 + 文件列表，供重试
      const reason = error instanceof Error ? error.message : "上传失败";
      setCreate({ name, files: create.files, workspaceId });
      setMessage(`工作区已创建，但文件上传失败（${reason}）。可重试上传或稍后在聊天中使用。`);
    } finally {
      setUploading(false);
    }
  }

  function cancelCreate() {
    if (uploading) return;
    setCreate(null);
  }

  function select(workspaceId: string) {
    localStorage.setItem(STORAGE_KEY, workspaceId);
    onSelect(workspaceId);
    setOpen(false);
  }

  async function deleteWorkspace() {
    if (!workspaceId) return;
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
      setMessage("删除失败。");
      return;
    }
    setWorkspaces((current) => current.filter((w) => w.workspaceId !== workspaceId));
    localStorage.removeItem(STORAGE_KEY);
    onSelect(null);
    onWorkspacesChanged?.();
  }

  return (
    <div className="relative inline-block" ref={menuRef}>
      <input
        ref={inputRef}
        className="hidden"
        type="file"
        multiple
        // @ts-expect-error webkitdirectory 是 Chromium 专有属性，非标准
        webkitdirectory=""
        onChange={onFilesPicked}
      />

      <button
        className="flex h-8 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 text-xs font-medium text-ink transition-colors hover:bg-surface-subtle"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <Folder className="size-3.5 text-accent" />
        <span className="max-w-32 truncate">{active?.name ?? "选择工作区"}</span>
        <ChevronDown className="size-3.5 text-muted" />
      </button>

      {open && (
        <div className="absolute bottom-9 left-0 z-20 min-w-56 rounded-xl border border-border bg-surface p-1.5 shadow-lg">
          {workspaces.length === 0 && (
            <p className="px-2.5 py-2 text-xs leading-5 text-muted">还没有工作区。</p>
          )}
          {workspaces.map((workspace) => (
            <button
              className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-ink transition-colors hover:bg-surface-subtle"
              key={workspace.workspaceId}
              type="button"
              onClick={() => select(workspace.workspaceId)}
            >
              <span className="flex min-w-0 items-center gap-2">
                <Folder className="size-3.5 shrink-0 text-muted" />
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{workspace.name}</span>
                  {workspace.localRoot && (
                    <span
                      className="max-w-40 truncate text-[10px] leading-3 text-faint"
                      title={workspace.localRoot}
                    >
                      本地 · {workspace.localRoot}
                    </span>
                  )}
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-1.5">
                {typeof workspace.metadata?.fileCount === "number" && (
                  <span className="text-[11px] text-faint">{workspace.metadata.fileCount} 文件</span>
                )}
                {workspace.workspaceId === workspaceId && (
                  <Check className="size-3.5 text-accent" />
                )}
              </span>
            </button>
          ))}
          <div className="my-1 border-t border-border" />
          <button
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs font-medium text-accent transition-colors hover:bg-surface-subtle"
            type="button"
            onClick={triggerFilePicker}
          >
            <FolderPlus className="size-3.5" />
            添加工作区…
          </button>
          <button
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs font-medium text-accent transition-colors hover:bg-surface-subtle"
            type="button"
            onClick={startBindLocal}
          >
            <Folder className="size-3.5" />
            新建并绑定本地目录…
          </button>
          {active && !active.localRoot && (
            <button
              className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs font-medium text-accent transition-colors hover:bg-surface-subtle"
              type="button"
              onClick={startBindCurrent}
            >
              <Folder className="size-3.5" />
              绑定「{active.name}」到本地目录…
            </button>
          )}
          {workspaceId && (
            <button
              className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-danger transition-colors hover:bg-surface-subtle"
              type="button"
              onClick={() => void deleteWorkspace()}
            >
              <Trash2 className="size-3.5" />
              删除当前工作区
            </button>
          )}
        </div>
      )}

      {create && (
        <div className="absolute bottom-12 left-0 z-30 min-w-64 rounded-xl border border-border bg-surface p-3 shadow-lg">
          <p className="text-xs font-medium text-ink">
            {create.targetWorkspaceId
              ? `绑定「${create.name}」到本地目录`
              : create.mode === "bind"
                ? "新建并绑定本地目录"
                : "创建工作区"}
          </p>
          {create.targetWorkspaceId ? (
            <p className="mt-1 text-[11px] leading-5 text-muted">
              填写该工作区对应的本机目录绝对路径；绑定后 Agent 的读写直接落在该目录。
              默认不搬运旧产物，绑定后会提示还有多少个旧文件未合并。
            </p>
          ) : create.mode === "bind" ? (
            <p className="mt-1 text-[11px] leading-5 text-muted">
              填写本机目录的绝对路径，Agent 将直接读写该目录（不再只写云端副本）。
            </p>
          ) : (
            <p className="mt-1 text-[11px] leading-5 text-muted">
              检测到文件夹 {create.name}，包含 {create.files.length} 个文件
              {create.workspaceId ? "（文件将上传到云端工作区）" : "（文件将真实上传到云端）"}。
            </p>
          )}
          {!create.targetWorkspaceId && (
            <input
              className="ui-field mt-2 h-8 w-full px-2.5 text-xs"
              placeholder="工作区名称"
              value={create.name}
              disabled={uploading}
              autoFocus={create.mode !== "bind"}
              onChange={(event) => setCreate({ ...create, name: event.target.value })}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void createWorkspace();
                }
              }}
            />
          )}
          {create.mode === "bind" && (
            <input
              className="ui-field mt-2 h-8 w-full px-2.5 text-xs"
              placeholder="本地目录绝对路径，如 D:\\agent\\Data"
              value={create.localRoot ?? ""}
              disabled={uploading}
              autoFocus
              spellCheck={false}
              onChange={(event) => {
                const localRoot = event.target.value;
                setCreate((current) => {
                  if (!current) return current;
                  const prevAuto = localDirName(current.localRoot ?? "");
                  const nextAuto = localDirName(localRoot);
                  // 名称未被手动改过时，随目录名自动填充
                  const name =
                    !current.name || current.name === prevAuto ? nextAuto : current.name;
                  return { ...current, localRoot, name };
                });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void createWorkspace();
                }
              }}
            />
          )}
          {create.mode === "bind" && create.targetWorkspaceId && (
            <label className="mt-2 flex items-start gap-2 text-[11px] leading-4 text-muted">
              <input
                type="checkbox"
                className="mt-0.5 size-3.5 shrink-0"
                checked={create.mergeExisting === true}
                disabled={uploading}
                onChange={(event) =>
                  setCreate((current) =>
                    current ? { ...current, mergeExisting: event.target.checked } : current,
                  )
                }
              />
              <span>
                合并存量文件：把该工作区旧副本里本机没有的文件复制过来（只新增、不覆盖已有文件）
              </span>
            </label>
          )}
          <div className="mt-2 flex justify-end gap-2">
            <button
              className="ui-button-secondary h-7 px-2.5 text-xs"
              type="button"
              disabled={uploading}
              onClick={cancelCreate}
            >
              取消
            </button>
            <button
              className="ui-button-primary flex h-7 items-center gap-1.5 px-2.5 text-xs"
              type="button"
              disabled={uploading}
              onClick={() => void createWorkspace()}
            >
              {uploading ? (
                <>
                  <LoaderCircle className="size-3.5 animate-spin" />
                  {create.mode === "bind" ? "绑定中…" : "上传中…"}
                </>
              ) : create.mode === "bind" ? (
                "绑定目录"
              ) : create.workspaceId ? (
                "重试上传"
              ) : (
                "创建并上传"
              )}
            </button>
          </div>
        </div>
      )}

      {message && (
        <div className="absolute bottom-9 left-0 z-20 max-w-72 whitespace-normal rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[11px] leading-4 text-muted shadow-sm">
          {message}
        </div>
      )}
    </div>
  );
});
