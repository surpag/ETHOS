import { auth } from "@clerk/nextjs/server";

import { forwardRuntime, readJSON } from "@/lib/models/runtime";
import { defaultContextWindow } from "@/lib/models/type";

// POST /api/custom-models —— 在某个供应商下新增模型。
// contextWindow 必填（缺省 128k）：它决定历史摘要阈值与工具结果预算。
export async function POST(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const body = await readJSON(request);
  if (!body) return Response.json({ error: "Invalid body" }, { status: 400 });

  const providerId = typeof body.providerId === "string" ? body.providerId.trim() : "";
  const apiModel = typeof body.apiModel === "string" ? body.apiModel.trim() : "";
  if (!providerId || !apiModel) {
    return Response.json({ error: "供应商与模型名称必填。" }, { status: 400 });
  }

  const contextWindow =
    typeof body.contextWindow === "number" && Number.isFinite(body.contextWindow)
      ? Math.round(body.contextWindow)
      : defaultContextWindow;

  return forwardRuntime(
    "/internal/custom-models",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        userId,
        providerId,
        apiModel,
        name: typeof body.name === "string" ? body.name.trim() : "",
        contextWindow,
        vision: body.vision === true,
      }),
    },
    request,
  );
}
