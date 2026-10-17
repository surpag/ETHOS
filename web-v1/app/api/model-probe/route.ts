import { auth } from "@clerk/nextjs/server";

import { forwardRuntime, readJSON } from "@/lib/models/runtime";

// 后端路径与前端动作名不是一一对应（test → probe），必须显式映射。
// 曾经直接拼 `/internal/custom-models/${action}`，POST .../test 命中了
// `/internal/custom-models/{modelId}` 的路径模板，返回 405 —— 表现为「连接失败」。
const RUNTIME_PATH = {
  discover: "/internal/custom-models/discover",
  test: "/internal/custom-models/probe",
} as const;

// POST /api/model-probe —— 端点探测，两种动作共用一条通道：
//   { action: "discover", providerId? | baseUrl+apiKey }        → 远端模型名列表
//   { action: "test", apiModel, providerId? | baseUrl+apiKey }  → 发一次 1-token 请求
// 探测目标可以是已保存的供应商（只传 providerId，凭据在服务端），
// 也可以是尚未保存的草稿（直接带 baseUrl + apiKey）。
export async function POST(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const body = await readJSON(request);
  if (!body) return Response.json({ error: "Invalid body" }, { status: 400 });

  const action = body.action === "test" ? "test" : body.action === "discover" ? "discover" : "";
  if (!action) return Response.json({ error: "action must be discover or test" }, { status: 400 });

  const payload: Record<string, unknown> = {
    userId,
    providerId: typeof body.providerId === "string" ? body.providerId.trim() : "",
    baseUrl: typeof body.baseUrl === "string" ? body.baseUrl.trim() : "",
    apiKey: typeof body.apiKey === "string" ? body.apiKey.trim() : "",
  };
  if (action === "test") {
    const apiModel = typeof body.apiModel === "string" ? body.apiModel.trim() : "";
    if (!apiModel) return Response.json({ error: "模型名称必填。" }, { status: 400 });
    payload.apiModel = apiModel;
  }

  return forwardRuntime(
    RUNTIME_PATH[action],
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    request,
  );
}
