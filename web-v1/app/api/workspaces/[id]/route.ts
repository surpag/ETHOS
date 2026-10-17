import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// DELETE /api/workspaces/{id} 删除工作区（级联删除指令）
export async function DELETE(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  const url = new URL(`/internal/workspaces/${encodeURIComponent(id)}`, runtimeURL);
  url.searchParams.set("userId", userId);
  return forward(url, { method: "DELETE" }, request);
}

// 本次新增：PATCH /api/workspaces/{id} 重命名工作区
export async function PATCH(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  const body = await request.json().catch(() => null) as { name?: string } | null;
  if (!body || typeof body.name !== "string") {
    return Response.json({ error: "name is required" }, { status: 400 });
  }
  const url = new URL(`/internal/workspaces/${encodeURIComponent(id)}`, runtimeURL);
  return forward(
    url,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, name: body.name }),
    },
    request,
  );
}

async function forward(url: string | URL, init?: RequestInit, request?: Request) {
  const requestId =
    request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      ...init,
      headers: { ...init?.headers, "X-Request-ID": requestId },
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
        "X-Request-ID": response.headers.get("X-Request-ID") ?? requestId,
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}
