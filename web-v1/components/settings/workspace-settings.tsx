"use client";

import { BookOpen, Save, Send, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

const STORAGE_KEY = "ethos.workspaceId";
// 聊天页写入的当前会话 ID；推送需要它来定位「哪个沙箱里的仓库」
const SESSION_KEY = "ethos.sessionId";

type Workspace = {
  workspaceId: string;
  name: string;
  // 沙箱出口白名单（空 = 全放通）；存于 metadata.allowedDomains，最多 10 个
  allowedDomains?: string[];
  // D-a：绑定的 Git 远端仓库地址（空 = 未绑定）
  gitRemoteUrl?: string;
};

// 工作区指令编辑器（设置页）：只编辑「已有工作区」的指令，不在此创建。
// 工作区创建入口在聊天页顶部下拉（主 UI）。
export function WorkspaceSettings() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [instruction, setInstruction] = useState("");
  const [domains, setDomains] = useState("");
  const [gitRemote, setGitRemote] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    async function loadWorkspaces() {
      const response = await fetch("/api/workspaces");
      if (!response.ok) {
        setMessage("无法加载工作区。");
        return;
      }
      const value = (await response.json()) as { workspaces: Workspace[] };
      setWorkspaces(value.workspaces);
      const saved = localStorage.getItem(STORAGE_KEY);
      const match = value.workspaces.find((w) => w.workspaceId === saved);
      if (match) {
        setSelectedId(match.workspaceId);
        setDomains((match.allowedDomains ?? []).join(", "));
        setGitRemote(match.gitRemoteUrl ?? "");
        void loadInstruction(match.workspaceId);
      }
    }
    void loadWorkspaces();
  }, []);

  async function loadInstruction(workspaceId: string) {
    const response = await fetch(`/api/workspaces/${encodeURIComponent(workspaceId)}/instructions`);
    if (!response.ok) {
      setInstruction("");
      return;
    }
    const value = (await response.json()) as { content: string };
    setInstruction(value.content);
  }

  function selectWorkspace(workspaceId: string) {
    setSelectedId(workspaceId);
    localStorage.setItem(STORAGE_KEY, workspaceId);
    const match = workspaces.find((w) => w.workspaceId === workspaceId);
    setDomains((match?.allowedDomains ?? []).join(", "));
    setGitRemote(match?.gitRemoteUrl ?? "");
    void loadInstruction(workspaceId);
    setMessage("已切换工作区，聊天将自动带上该工作区指令。");
  }

  async function saveDomains() {
    if (!selectedId) return;
    const list = domains
      .split(/[,;\s]+/)
      .map((d) => d.trim())
      .filter(Boolean);
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(selectedId)}/allowed-domains`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domains: list }),
      },
    );
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      setMessage(body?.message ?? "保存白名单失败：请检查域名格式与数量（最多 10 个）。");
      return;
    }
    setWorkspaces((current) =>
      current.map((w) => (w.workspaceId === selectedId ? { ...w, allowedDomains: list } : w)),
    );
    setMessage(
      list.length
        ? `白名单已保存（${list.length} 个域名），下一次沙箱连接时生效。`
        : "白名单已清空，沙箱恢复全放通。",
    );
  }

  async function saveGitRemote() {
    if (!selectedId) return;
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(selectedId)}/git-remote`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remoteUrl: gitRemote.trim() }),
      },
    );
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      setMessage(body?.message ?? "保存仓库地址失败：请检查是否为公网 https/ssh 地址。");
      return;
    }
    const value = (await response.json()) as { remoteUrl: string };
    setWorkspaces((current) =>
      current.map((w) =>
        w.workspaceId === selectedId ? { ...w, gitRemoteUrl: value.remoteUrl } : w,
      ),
    );
    setMessage(value.remoteUrl ? "仓库地址已保存。" : "已解绑仓库。");
  }

  async function pushToRemote() {
    if (!selectedId) return;
    const sessionId = localStorage.getItem(SESSION_KEY) ?? "";
    if (!sessionId) {
      setMessage("找不到当前会话：请先在聊天页与 Agent 交互一次，再回来推送。");
      return;
    }
    setMessage("正在推送…");
    const response = await fetch("/api/git/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, workspaceId: selectedId }),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      setMessage(body?.message ?? "推送失败。");
      return;
    }
    const value = (await response.json()) as { branch: string };
    setMessage(`已推送到远端（分支 ${value.branch}）。`);
  }

  async function saveInstruction() {    if (!selectedId) return;
    const response = await fetch(`/api/workspaces/${encodeURIComponent(selectedId)}/instructions`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: instruction }),
    });
    if (!response.ok) {
      setMessage("保存指令失败。");
      return;
    }
    setMessage("指令已保存，Agent 每次对话会自动读取。");
  }

  async function clearInstruction() {
    if (!selectedId) return;
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(selectedId)}/instructions`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      setMessage("清空失败。");
      return;
    }
    setInstruction("");
    setMessage("指令已清空。");
  }

  return (
    <section className="ui-surface overflow-hidden">
      <header className="flex items-start gap-3 border-b border-border px-5 py-4">
        <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
          <BookOpen className="size-4" />
        </span>
        <div>
          <h2 className="text-sm font-semibold text-ink">工作区指令</h2>
          <p className="mt-1 text-xs leading-5 text-muted">
            为已有工作区编写项目指令（等价 AGENTS.md），Agent 每次对话自动读取。
            新建工作区请在聊天页顶部完成。
          </p>
        </div>
      </header>

      <div className="space-y-3 p-4">
        {workspaces.length === 0 ? (
          <p className="text-xs leading-5 text-muted">
            还没有工作区。请回到聊天页，在顶部「添加工作区」创建一个。
          </p>
        ) : (
          <select
            className="ui-field h-9 w-full px-3"
            value={selectedId}
            onChange={(event) => selectWorkspace(event.target.value)}
          >
            <option value="">选择一个工作区…</option>
            {workspaces.map((workspace) => (
              <option key={workspace.workspaceId} value={workspace.workspaceId}>
                {workspace.name}
              </option>
            ))}
          </select>
        )}

        {selectedId && (
          <>
            <label className="block text-xs font-medium text-muted">
              项目指令（Agent 每次对话自动读取）
              <textarea
                className="ui-field mt-2 min-h-40 w-full p-3 text-sm leading-6"
                placeholder={"在这里写下项目的技术栈、代码规范、常用命令等，例如：\n\n# 项目背景\n技术栈：FastAPI + SQLite\n规范：函数必须带 docstring\n常用命令：uv run uvicorn ... --port 8080"}
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
              />
            </label>
            <div className="flex items-center gap-2">
              <button className="ui-button-primary" type="button" onClick={() => void saveInstruction()}>
                <Save className="size-4" />
                保存指令
              </button>
              <button className="ui-button-secondary" type="button" onClick={() => void clearInstruction()}>
                <Trash2 className="size-4" />
                清空
              </button>
            </div>

            <label className="block text-xs font-medium text-muted">
              沙箱出口白名单（可选，最多 10 个域名，逗号分隔；留空 = 全放通）
              <input
                className="ui-field mt-2 h-9 w-full px-3 text-sm"
                placeholder="pypi.org, files.pythonhosted.org, *.github.com"
                value={domains}
                spellCheck={false}
                onChange={(event) => setDomains(event.target.value)}
              />
            </label>
            <p className="text-[11px] leading-4 text-muted">
              配置后沙箱只能访问这些域名（HTTP/HTTPS）。用于限制 Agent 联网范围、
              防止数据外泄；安装依赖请记得放行对应源（如 pypi.org、registry.npmjs.org）。
              下一次沙箱连接时生效。
            </p>
            <button className="ui-button-secondary" type="button" onClick={() => void saveDomains()}>
              <Save className="size-4" />
              保存白名单
            </button>

            <label className="block text-xs font-medium text-muted">
              Git 远端仓库（可选；如 https://github.com/you/repo.git）
              <input
                className="ui-field mt-2 h-9 w-full px-3 text-sm"
                placeholder="https://github.com/you/repo.git"
                value={gitRemote}
                spellCheck={false}
                onChange={(event) => setGitRemote(event.target.value)}
              />
            </label>
            <div className="flex items-center gap-2">
              <button className="ui-button-secondary" type="button" onClick={() => void saveGitRemote()}>
                <Save className="size-4" />
                保存仓库地址
              </button>
              <button
                className="ui-button-primary"
                type="button"
                disabled={!gitRemote.trim()}
                onClick={() => void pushToRemote()}
              >
                <Send className="size-4" />
                推送到远端
              </button>
            </div>
            <p className="text-[11px] leading-4 text-muted">
              推送会把当前会话沙箱里该工作区的提交推到上面的仓库（凭据只在服务端使用）。
              需先在「设置 → Git 仓库」连接 GitHub。
            </p>
          </>
        )}

        {message && <p className="text-xs leading-5 text-muted">{message}</p>}
      </div>
    </section>
  );
}
