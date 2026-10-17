export type ProviderInfo = {
  id: string;
  name: string;
  // 自定义供应商才有值；内置供应商的 baseUrl 只在服务端，不下发到浏览器
  baseUrl?: string;
  custom?: boolean;
  modelCount?: number;
};

export type ReasoningOption = { id: string; name: string };
export type ModelCapabilities = { vision: boolean };

export type ModelInfo = {
  id: string;
  providerId: string;
  name: string;
  reasoningOptions: ReasoningOption[];
  capabilities: ModelCapabilities;
  // 自定义模型为 true；内置模型只读，不可删除
  custom?: boolean;
  contextWindow?: number;
};

export type ModelCatalogResponse = { providers: ProviderInfo[]; models: ModelInfo[] };
export type AvailableModelCatalogResponse = { models: ModelInfo[] };

// 上下文窗口档位。不提供「未知」选项：它决定历史摘要阈值与工具预算，
// 填 0 会让运行时退化成「每轮都压缩」。
export const contextWindowPresets = [
  { label: "32k", value: 32_000 },
  { label: "128k", value: 128_000 },
  { label: "1M", value: 1_000_000 },
] as const;

export const defaultContextWindow = 128_000;

export type CreateProviderPayload = {
  name: string;
  baseUrl: string;
  apiKey: string;
};

export type CreateModelPayload = {
  providerId: string;
  apiModel: string;
  name?: string;
  contextWindow: number;
  vision?: boolean;
};

export type ProbePayload = {
  apiModel: string;
  providerId?: string;
  baseUrl?: string;
  apiKey?: string;
};

export type ProbeResult = { ok: boolean; latencyMs: number };

export function formatContextWindow(value?: number): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 === 0 ? 0 : 1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
}
