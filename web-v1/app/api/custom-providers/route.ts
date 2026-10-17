import { auth } from "@clerk/nextjs/server";

import { forwardRuntime, readJSON } from "@/lib/models/runtime";

// POST /api/custom-providers —— 新建自定义供应商（兼容 OpenAI 协议的端点）。
// API Key 只上送服务端并写进 user_providers，浏览器不回读。
export async function POST(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const body = await readJSON(request);
  if (!body) return Response.json({ error: "Invalid body" }, { status: 400 });

  const name = typeof body.name === "string" ? body.name.trim() : "";
  const baseUrl = typeof body.baseUrl === "string" ? body.baseUrl.trim() : "";
  const apiKey = typeof body.apiKey === "string" ? body.apiKey.trim() : "";
  if (!name || !baseUrl) {
    return Response.json({ error: "名称与 Base URL 必填。" }, { status: 400 });
  }

  return forwardRuntime(
    "/internal/custom-providers",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, name, baseUrl, apiKey }),
    },
    request,
  );
}
