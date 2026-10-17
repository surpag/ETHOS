"use client";

import { CheckCircle2, GitBranch, KeyRound, Plus, Save, SlidersHorizontal, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

// browserTimezone 是用户浏览器的本地时区，首次设置时作为默认值。
function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  } catch {
    return "Asia/Shanghai";
  }
}

// commonTimezones 是常见 IANA 时区，保持列表精简。
const commonTimezones = [
  "Asia/Shanghai",
  "Asia/Hong_Kong",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Europe/London",
  "Europe/Paris",
  "America/New_York",
  "America/Los_Angeles",
  "Australia/Sydney",
  "UTC",
];

import type { ModelCatalogResponse, ModelInfo, ProviderInfo } from "@/lib/models/type";
import { formatContextWindow } from "@/lib/models/type";
import { notifyModelsChanged } from "@/lib/models/events";
import type { UserSettingsResponse } from "@/lib/userconfig/type";
import { CustomModelDialog } from "./custom-model-dialog";
import { WorkspaceSettings } from "./workspace-settings";

const NEW_PROVIDER = "__new__";

export function SettingsForm() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [configured, setConfigured] = useState<Record<string, boolean>>({});
  const [personality, setPersonality] = useState("");
  const [defaultModelId, setDefaultModelId] = useState("");
  const [timezone, setTimezone] = useState("");
  const [gitAuthorName, setGitAuthorName] = useState("");
  const [gitAuthorEmail, setGitAuthorEmail] = useState("");
  const [message, setMessage] = useState("");
  const [gitToken, setGitToken] = useState("");
  const [gitConnected, setGitConnected] = useState(false);
  const [gitMessage, setGitMessage] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogProviderId, setDialogProviderId] = useState("");
  // 每次打开都换 key，强制弹窗重挂载 → 内部状态自然回到初始值，
  // 不需要在 effect 里逐个 setState（那会触发级联渲染）。
  const [dialogKey, setDialogKey] = useState(0);

  // 目录与自定义条目会在同一个页面上被改动（加模型、删模型、删供应商），
  // 抽成一个可重复调用的加载函数，避免各处手写刷新逻辑而漏掉某一块。
  const loadCatalog = useCallback(async () => {
    const response = await fetch("/api/models");
    if (!response.ok) {
      setMessage("无法加载模型目录。");
      return;
    }
    const catalog = await response.json() as ModelCatalogResponse;
    setProviders(catalog.providers);
    setModels(catalog.models);
  }, []);

  useEffect(() => {
    async function load() {
      const [modelsResponse, settingsResponse, gitResponse] = await Promise.all([
        fetch("/api/models"),
        fetch("/api/settings"),
        fetch("/api/git-credentials"),
      ]);
      if (!modelsResponse.ok || !settingsResponse.ok) {
        setMessage("无法加载设置。");
        return;
      }
      if (gitResponse.ok) {
        const git = await gitResponse.json() as { connected: string[] };
        setGitConnected(git.connected.includes("github"));
      }

      const catalog = await modelsResponse.json() as ModelCatalogResponse;
      const settings = await settingsResponse.json() as UserSettingsResponse;
      setProviders(catalog.providers);
      setModels(catalog.models);
      setPersonality(settings.personality);
      setDefaultModelId(settings.defaultModelId);
      setTimezone(settings.timezone || browserTimezone());
      setGitAuthorName(settings.gitAuthorName ?? "");
      setGitAuthorEmail(settings.gitAuthorEmail ?? "");
      setConfigured(Object.fromEntries(
        settings.providers.map((provider) => [provider.providerId, provider.hasApiKey]),
      ));
    }
    void load();
  }, []);

  // 模型集合发生变化时：本页刷新目录 + 广播给聊天页（它的模型下拉不会自动重挂载）
  const refreshModels = useCallback(async () => {
    await loadCatalog();
    notifyModelsChanged();
  }, [loadCatalog]);

  function openDialog(providerId: string) {
    setDialogProviderId(providerId);
    setDialogKey((current) => current + 1);
    setDialogOpen(true);
  }

  async function deleteModel(model: ModelInfo) {
    const response = await fetch(`/api/custom-models/${encodeURIComponent(model.id)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setMessage((await response.text()) || "删除模型失败。");
      return;
    }
    if (defaultModelId === model.id) {
      setMessage("该模型是当前默认模型，已删除；请重新选择默认模型。");
    }
    await refreshModels();
  }

  async function deleteProvider(provider: ProviderInfo) {
    const owned = models.filter((model) => model.providerId === provider.id);
    const confirmed = window.confirm(
      `删除供应商「${provider.name}」会同时移除它的 ${owned.length} 个模型与已保存的 API Key，确定继续？`,
    );
    if (!confirmed) return;

    const response = await fetch(`/api/custom-providers/${encodeURIComponent(provider.id)}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setMessage((await response.text()) || "删除供应商失败。");
      return;
    }
    await refreshModels();
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const providerKeys = providers.flatMap((provider) => {
      const apiKey = keys[provider.id]?.trim();
      return apiKey ? [{ providerId: provider.id, apiKey }] : [];
    });
    const response = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ personality, defaultModelId, timezone, gitAuthorName, gitAuthorEmail, providers: providerKeys }),
    });
    if (!response.ok) {
      setMessage((await response.text()) || "保存设置失败。");
      return;
    }

    const saved = await response.json() as UserSettingsResponse;
    setConfigured(Object.fromEntries(
      saved.providers.map((provider) => [provider.providerId, provider.hasApiKey]),
    ));
    setDefaultModelId(saved.defaultModelId);
    setKeys({});
    // Key 落库会改变「可用模型」集合（available-models 按供应商是否有 Key 过滤）
    notifyModelsChanged();
    setMessage("已保存。");
  }

  // Git 凭据独立保存（不走表单提交）：token 只上送服务端，用于 git push / pull。
  async function saveGitCredential() {
    const token = gitToken.trim();
    if (!token) {
      setGitMessage("请输入 Token。");
      return;
    }
    const response = await fetch("/api/git-credentials", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: "github", token }),
    });
    if (!response.ok) {
      setGitMessage((await response.text()) || "保存失败。");
      return;
    }
    const saved = await response.json() as { connected: string[] };
    setGitConnected(saved.connected.includes("github"));
    setGitToken("");
    setGitMessage("已连接 GitHub。");
  }

  async function disconnectGit() {
    const response = await fetch("/api/git-credentials?provider=github", {
      method: "DELETE",
    });
    if (!response.ok) {
      setGitMessage("断开失败。");
      return;
    }
    const saved = await response.json() as { connected: string[] };
    setGitConnected(saved.connected.includes("github"));
    setGitMessage("已断开 GitHub。");
  }

  return (
    <div className="p-6">
      <form className="space-y-5" onSubmit={save}>
        <section className="ui-surface overflow-hidden">
          <header className="flex items-start gap-3 border-b border-border px-5 py-4">
            <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><KeyRound className="size-4" /></span>
            <div><h2 className="text-sm font-semibold text-ink">模型供应商</h2><p className="mt-1 text-xs leading-5 text-muted">凭据只存放在服务端；配置后模型才会出现在聊天页。</p></div>
          </header>
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            {providers.map((provider) => {
              const owned = models.filter((model) => model.providerId === provider.id);
              const isExpanded = expanded[provider.id] ?? false;
              return (
                <div className="rounded-xl border border-border bg-surface-subtle p-3.5 transition-colors focus-within:border-accent" key={provider.id}>
                  <div className="mb-3 flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate text-sm font-medium text-ink">{provider.name}</span>
                        {configured[provider.id] && <span className="inline-flex items-center gap-1 text-[11px] font-medium text-success"><CheckCircle2 className="size-3.5" />已配置</span>}
                        {provider.custom && <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">自定义</span>}
                      </div>
                      {provider.custom && provider.baseUrl && <p className="mt-1 truncate text-[11px] text-muted">{provider.baseUrl}</p>}
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <button
                        className="rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-muted transition-colors hover:text-ink"
                        type="button"
                        onClick={() => setExpanded((current) => ({ ...current, [provider.id]: !isExpanded }))}
                      >
                        {owned.length} 个模型
                      </button>
                      {provider.custom && (
                        <button className="ui-icon-button" type="button" aria-label={`删除 ${provider.name}`} onClick={() => void deleteProvider(provider)}>
                          <Trash2 className="size-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                  <input
                    aria-label={`${provider.name} API Key`}
                    className="ui-field h-9 px-3"
                    placeholder={configured[provider.id] ? "留空则不修改" : "输入 API Key"}
                    type="password"
                    value={keys[provider.id] ?? ""}
                    onChange={(event) => setKeys((current) => ({ ...current, [provider.id]: event.target.value }))}
                  />
                  {isExpanded && (
                    <div className="mt-3 border-t border-border pt-2">
                      {owned.length === 0 && <p className="py-1 text-[11px] text-muted">还没有模型。</p>}
                      {owned.map((model) => (
                        <div className="flex items-center justify-between gap-2 py-1" key={model.id}>
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="truncate text-xs text-ink">{model.name}</span>
                            <span className="shrink-0 text-[11px] text-muted">{formatContextWindow(model.contextWindow)}</span>
                            {model.id === defaultModelId && <span className="shrink-0 rounded bg-warning-soft px-1 text-[10px] text-warning">默认</span>}
                          </div>
                          {model.custom
                            ? <button className="shrink-0 text-[11px] text-danger hover:underline" type="button" onClick={() => void deleteModel(model)}>删除</button>
                            : <span className="shrink-0 text-[11px] text-faint">内置</span>}
                        </div>
                      ))}
                      <button className="mt-1 text-xs text-accent hover:underline" type="button" onClick={() => openDialog(provider.id)}>+ 添加模型</button>
                    </div>
                  )}
                </div>
              );
            })}
            <button
              className="flex min-h-[72px] items-center justify-center gap-1.5 rounded-xl border border-dashed border-border bg-transparent p-3.5 text-xs text-accent transition-colors hover:bg-accent-soft"
              type="button"
              onClick={() => openDialog(NEW_PROVIDER)}
            >
              <Plus className="size-3.5" />添加供应商
            </button>
          </div>
          <p className="border-t border-border px-5 py-3 text-[11px] leading-5 text-muted">兼容 OpenAI 协议的端点都可以接入；Base URL 与 Key 只存在服务端，不会下发到沙箱。</p>
        </section>

        <section className="ui-surface overflow-hidden">
          <header className="flex items-start gap-3 border-b border-border px-5 py-4">
            <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"><GitBranch className="size-4" /></span>
            <div><h2 className="text-sm font-semibold text-ink">Git 仓库</h2><p className="mt-1 text-xs leading-5 text-muted">Token 只存放在服务端，用于拉取 / 推送；不会下发到 Agent 沙箱。</p></div>
          </header>
          <div className="space-y-2 p-4">
            <label className="block text-xs font-medium text-muted">GitHub Personal Access Token
              <div className="mt-2 flex items-center gap-2">
                <input className="ui-field h-9 min-w-0 flex-1 px-3" placeholder={gitConnected ? "已连接；重填可覆盖" : "github_pat_… 或 ghp_…"} type="password" value={gitToken} onChange={(event) => setGitToken(event.target.value)} />
                <button className="ui-button-primary h-9 shrink-0" type="button" onClick={() => void saveGitCredential()}>{gitConnected ? "更新" : "连接"}</button>
                {gitConnected && <button className="ui-button-secondary h-9 shrink-0" type="button" onClick={() => void disconnectGit()}>断开</button>}
              </div>
            </label>
            <p className="text-xs leading-5 text-muted">
              {gitConnected && <span className="mr-1 inline-flex items-center gap-1 font-medium text-success"><CheckCircle2 className="size-3.5" />已连接 GitHub</span>}
              {gitMessage || "连接后可让 Agent 在工作区内拉取、提交与推送代码。"}
            </p>
          </div>
        </section>

        <section className="ui-surface overflow-hidden">
          <header className="flex items-start gap-3 border-b border-border px-5 py-4">
            <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-warning-soft text-warning"><SlidersHorizontal className="size-4" /></span>
            <div><h2 className="text-sm font-semibold text-ink">Agent 偏好</h2><p className="mt-1 text-xs leading-5 text-muted">默认模型和时区会被 IM、定时任务等非聊天入口复用。</p></div>
          </header>
          <div className="grid gap-4 p-4 sm:grid-cols-2">
            <label className="block text-xs font-medium text-muted">默认模型
              <select className="ui-field mt-2 h-10 px-3" value={defaultModelId} onChange={(event) => setDefaultModelId(event.target.value)}>{models.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select>
            </label>
            <label className="block text-xs font-medium text-muted">时区
              <select className="ui-field mt-2 h-10 px-3" value={timezone} onChange={(event) => setTimezone(event.target.value)}>{commonTimezones.map((zone) => <option key={zone} value={zone}>{zone}</option>)}</select>
            </label>
            <label className="block text-xs font-medium text-muted">Git 提交作者名
              <input className="ui-field mt-2 h-10 px-3" placeholder="留空则用平台默认" value={gitAuthorName} onChange={(event) => setGitAuthorName(event.target.value)} />
            </label>
            <label className="block text-xs font-medium text-muted">Git 提交邮箱
              <input className="ui-field mt-2 h-10 px-3" placeholder="留空则用平台默认" value={gitAuthorEmail} onChange={(event) => setGitAuthorEmail(event.target.value)} />
            </label>
            <label className="block text-xs font-medium text-muted sm:col-span-2">个性化指令
              <textarea className="ui-field mt-2 min-h-32 p-3 text-sm leading-6" placeholder="告诉 ETHOS 你的偏好与沟通方式…" value={personality} onChange={(event) => setPersonality(event.target.value)} />
            </label>
          </div>
        </section>

        <WorkspaceSettings />

        <footer className="flex items-center justify-between gap-4 rounded-xl border border-border bg-surface-subtle px-4 py-3">
          <p className="min-w-0 text-sm text-muted">{message || "修改会在下一次请求中生效。"}</p>
          <button className="ui-button-primary shrink-0" type="submit"><Save className="size-4" />保存设置</button>
        </footer>

        <CustomModelDialog
          key={dialogKey}
          open={dialogOpen}
          providers={providers}
          defaultProviderId={dialogProviderId}
          onClose={() => setDialogOpen(false)}
          onSaved={() => { void refreshModels(); setMessage("模型已添加，可在聊天页选择。"); }}
        />
      </form>
    </div>
  );
}
