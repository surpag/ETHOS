"use client";

import { CheckCircle2, Loader2, X } from "lucide-react";
import { useState } from "react";

import {
  contextWindowPresets,
  defaultContextWindow,
  formatContextWindow,
  type ProviderInfo,
} from "@/lib/models/type";

const NEW_PROVIDER = "__new__";

type CustomModelDialogProps = {
  open: boolean;
  providers: ProviderInfo[];
  defaultProviderId?: string;
  onClose: () => void;
  onSaved: () => void;
};

type ProbeState = { ok: true; latencyMs: number } | { ok: false; message: string } | null;

// 一个弹窗覆盖两种场景：给已有供应商加模型（只填模型名），或先建供应商再接模型。
// 两者共用一个入口，是因为用户的心智是「我要加个模型」，而不是「我要先建供应商」。
export function CustomModelDialog({
  open,
  providers,
  defaultProviderId,
  onClose,
  onSaved,
}: CustomModelDialogProps) {
  // 初始值由父组件通过 key 重挂载来重置：不用 effect 里 setState（会级联渲染），
  // 也避免「关掉再打开残留上一次的选择」。
  const [providerId, setProviderId] = useState(
    defaultProviderId || providers[0]?.id || NEW_PROVIDER,
  );
  const [newName, setNewName] = useState("");
  const [newBaseUrl, setNewBaseUrl] = useState("");
  const [newApiKey, setNewApiKey] = useState("");
  const [apiModel, setApiModel] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [contextWindow, setContextWindow] = useState<number>(defaultContextWindow);
  const [vision, setVision] = useState(false);
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<"" | "discover" | "test" | "save">("");
  const [probe, setProbe] = useState<ProbeState>(null);
  const [error, setError] = useState("");

  if (!open) return null;

  const creatingProvider = providerId === NEW_PROVIDER;

  // 输入框兼作搜索框：模型名精确命中某项时不再过滤（否则选中后列表只剩一行，
  // 想换成别的还得先清空）；否则按子串匹配，能让 100+ 项的长列表立刻收敛。
  const visibleModels = (discovered ?? []).includes(apiModel)
    ? discovered ?? []
    : (discovered ?? []).filter((name) =>
        name.toLowerCase().includes(apiModel.trim().toLowerCase()),
      );

  function target() {
    if (creatingProvider) {
      return { baseUrl: newBaseUrl.trim(), apiKey: newApiKey.trim() };
    }
    return { providerId };
  }

  async function discover() {
    setError("");
    setProbe(null);
    setBusy("discover");
    try {
      const response = await fetch("/api/model-probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "discover", ...target() }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setError(readError(payload, "拉取模型列表失败。"));
        return;
      }
      const models = Array.isArray(payload?.models) ? (payload.models as string[]) : [];
      setDiscovered(models);
      if (!models.length) setError("端点没有返回任何模型，请直接手填模型名称。");
    } finally {
      setBusy("");
    }
  }

  async function test() {
    setError("");
    setProbe(null);
    if (!apiModel.trim()) {
      setError("请先填写模型名称。");
      return;
    }
    setBusy("test");
    try {
      const response = await fetch("/api/model-probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "test", apiModel: apiModel.trim(), ...target() }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setProbe({ ok: false, message: readError(payload, "连接失败。") });
        return;
      }
      setProbe({ ok: true, latencyMs: Number(payload?.latencyMs ?? 0) });
    } finally {
      setBusy("");
    }
  }

  async function save() {
    setError("");
    if (!apiModel.trim()) {
      setError("请先填写模型名称。");
      return;
    }
    if (creatingProvider && (!newName.trim() || !newBaseUrl.trim())) {
      setError("新建供应商需要填写名称与 Base URL。");
      return;
    }
    setBusy("save");
    try {
      let targetProviderId = providerId;
      if (creatingProvider) {
        const created = await fetch("/api/custom-providers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: newName.trim(),
            baseUrl: newBaseUrl.trim(),
            apiKey: newApiKey.trim(),
          }),
        });
        const payload = await created.json().catch(() => null);
        if (!created.ok) {
          setError(readError(payload, "新建供应商失败。"));
          return;
        }
        targetProviderId = String(payload?.id ?? "");
        if (!targetProviderId) {
          setError("新建供应商失败：服务端未返回 id。");
          return;
        }
      }

      const response = await fetch("/api/custom-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          providerId: targetProviderId,
          apiModel: apiModel.trim(),
          name: displayName.trim(),
          contextWindow,
          vision,
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setError(readError(payload, "添加模型失败。"));
        return;
      }
      onSaved();
      onClose();
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="添加模型">
      <section className="w-full max-w-lg overflow-hidden rounded-2xl border border-border bg-canvas shadow-2xl shadow-black/25">
        <header className="flex items-center justify-between border-b border-border bg-surface px-5 py-4">
          <div>
            <h2 className="text-sm font-semibold text-ink">添加模型</h2>
            <p className="mt-0.5 text-xs text-muted">兼容 OpenAI 协议的端点都可用；名称填 API 的真实 model 名。</p>
          </div>
          <button className="ui-icon-button" onClick={onClose} type="button" aria-label="关闭"><X className="size-4" /></button>
        </header>

        <div className="max-h-[min(560px,calc(100vh-12rem))] space-y-4 overflow-y-auto p-5">
          <label className="block text-xs font-medium text-muted">供应商
            <select className="ui-field mt-2 h-10 px-3" value={providerId} onChange={(event) => { setProviderId(event.target.value); setDiscovered(null); setProbe(null); setError(""); }}>
              {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
              <option value={NEW_PROVIDER}>+ 新建供应商…</option>
            </select>
          </label>

          {creatingProvider && (
            <div className="grid gap-3 rounded-xl border border-border bg-surface-subtle p-3.5">
              <label className="block text-xs font-medium text-muted">名称
                <input className="ui-field mt-1.5 h-9 px-3" placeholder="如 Moonshot" value={newName} onChange={(event) => setNewName(event.target.value)} />
              </label>
              <label className="block text-xs font-medium text-muted">Base URL
                <input className="ui-field mt-1.5 h-9 px-3" placeholder="https://api.moonshot.cn/v1" value={newBaseUrl} onChange={(event) => setNewBaseUrl(event.target.value)} />
              </label>
              <label className="block text-xs font-medium text-muted">API Key
                <input className="ui-field mt-1.5 h-9 px-3" placeholder="sk-…" type="password" value={newApiKey} onChange={(event) => setNewApiKey(event.target.value)} />
              </label>
              <p className="text-[11px] leading-5 text-muted">Key 只存服务端，不会下发到 Agent 沙箱。</p>
            </div>
          )}

          <div>
            <label className="block text-xs font-medium text-muted">模型名称
              <div className="mt-2 flex items-center gap-2">
                <input className="ui-field h-9 min-w-0 flex-1 px-3" placeholder="如 deepseek-v4.1-nitro" value={apiModel} onChange={(event) => { setApiModel(event.target.value); setProbe(null); }} />
                <button className="ui-button-secondary h-9 shrink-0" type="button" disabled={busy !== ""} onClick={() => void discover()}>
                  {busy === "discover" ? <Loader2 className="size-4 animate-spin" /> : "拉取"}
                </button>
              </div>
            </label>
            {discovered && discovered.length > 0 && (
              <div className="mt-2">
                <p className="mb-1 text-[11px] text-muted">
                  端点返回 {discovered.length} 个模型{visibleModels.length !== discovered.length ? `，匹配 ${visibleModels.length} 个` : ""}；输入模型名可过滤
                </p>
                <div className="max-h-48 overflow-y-auto rounded-lg border border-border bg-surface-subtle">
                  {visibleModels.map((name) => (
                    <button
                      key={name}
                      className={`block w-full truncate px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent-soft ${name === apiModel ? "text-accent" : "text-ink"}`}
                      type="button"
                      onClick={() => { setApiModel(name); setProbe(null); }}
                    >
                      {name}
                    </button>
                  ))}
                  {visibleModels.length === 0 && <p className="px-3 py-2 text-xs text-muted">没有匹配的模型名。</p>}
                </div>
              </div>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs font-medium text-muted">显示名称
              <input className="ui-field mt-2 h-9 px-3" placeholder="可选，默认同模型名" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
            </label>
            <div className="block text-xs font-medium text-muted">上下文窗口
              <div className="mt-2 flex h-9 overflow-hidden rounded-lg border border-border bg-surface">
                {contextWindowPresets.map((preset) => (
                  <button
                    key={preset.value}
                    className={`flex-1 border-r border-border text-xs transition-colors last:border-r-0 ${contextWindow === preset.value ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-subtle"}`}
                    type="button"
                    onClick={() => setContextWindow(preset.value)}
                  >
                    {preset.label}
                  </button>
                ))}
                <input
                  className="w-20 border-l border-border bg-transparent px-2 text-center text-xs text-ink outline-none"
                  inputMode="numeric"
                  placeholder="自定义"
                  value={contextWindowPresets.some((item) => item.value === contextWindow) ? "" : contextWindow}
                  onChange={(event) => {
                    const value = Number(event.target.value.replace(/[^0-9]/g, ""));
                    setContextWindow(Number.isFinite(value) && value > 0 ? value : 0);
                  }}
                />
              </div>
              <p className="mt-1.5 text-[11px] leading-5 text-muted">当前 {formatContextWindow(contextWindow)}；它决定何时压缩历史。</p>
            </div>
          </div>

          <label className="flex cursor-pointer items-start gap-2 text-xs text-muted">
            <input className="mt-0.5 size-3.5 shrink-0 accent-[var(--accent)]" type="checkbox" checked={vision} onChange={(event) => setVision(event.target.checked)} />
            <span className="leading-5">支持图片输入（勾错会导致发送图片失败）</span>
          </label>

          {probe && (
            <div className={`rounded-lg border px-3 py-2 text-xs ${probe.ok ? "border-border bg-surface-subtle text-muted" : "border-warning/40 bg-warning-soft text-warning"}`}>
              {probe.ok ? (
                <span className="inline-flex items-center gap-1.5 text-success"><CheckCircle2 className="size-3.5" />连接正常 · 首字 {probe.latencyMs}ms</span>
              ) : (
                <div className="space-y-1">
                  <p>{probe.message}</p>
                  <p className="text-[11px] opacity-80">仍可直接添加，之后在聊天里验证（测试失败不阻塞保存）。</p>
                </div>
              )}
            </div>
          )}
          {error && <p className="text-xs leading-5 text-danger">{error}</p>}
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-border bg-surface px-5 py-3">
          <button className="ui-button-secondary h-9" type="button" disabled={busy !== ""} onClick={() => void test()}>
            {busy === "test" ? <Loader2 className="size-4 animate-spin" /> : "测试连接"}
          </button>
          <div className="flex items-center gap-2">
            <button className="ui-button-secondary h-9" type="button" onClick={onClose}>取消</button>
            <button className="ui-button-primary h-9" type="button" disabled={busy !== ""} onClick={() => void save()}>
              {busy === "save" ? <Loader2 className="size-4 animate-spin" /> : "添加"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function readError(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.error === "string" && record.error.trim()) return record.error;
  }
  return fallback;
}
