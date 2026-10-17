import { auth } from "@clerk/nextjs/server";

import { forwardRuntime, readJSON } from "@/lib/models/runtime";

// PATCH /api/custom-models/[modelId] —— 改显示名 / 模型名 / 上下文窗口 / 视觉能力。
export async function PATCH(
  request: Request,
  context: RouteContext<"/api/custom-models/[modelId]">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const body = await readJSON(request);
  if (!body) return Response.json({ error: "Invalid body" }, { status: 400 });

  const { modelId } = await context.params;
  const payload: Record<string, unknown> = { userId };
  if (typeof body.name === "string") payload.name = body.name.trim();
  if (typeof body.apiModel === "string") payload.apiModel = body.apiModel.trim();
  if (typeof body.contextWindow === "number" && Number.isFinite(body.contextWindow)) {
    payload.contextWindow = Math.round(body.contextWindow);
  }
  if (typeof body.vision === "boolean") payload.vision = body.vision;

  return forwardRuntime(
    `/internal/custom-models/${encodeURIComponent(modelId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
    request,
  );
}

// DELETE /api/custom-models/[modelId] —— 内置模型会被后端拒绝（400）。
export async function DELETE(
  request: Request,
  context: RouteContext<"/api/custom-models/[modelId]">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { modelId } = await context.params;
  return forwardRuntime(
    `/internal/custom-models/${encodeURIComponent(modelId)}?userId=${encodeURIComponent(userId)}`,
    { method: "DELETE" },
    request,
  );
}
