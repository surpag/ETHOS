import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/workspaces/{id}/instructions 读取工作区指令（编辑区回显）
export async function GET(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/instructions">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  const url = new URL(`/internal/workspaces/${encodeURIComponent(id)}/instructions`, runtimeURL);
  url.searchParams.set("userId", userId);
  return forward(url, { method: "GET" }, request);
}

// PUT /api/workspaces/{id}/instructions 整段覆盖保存指令 { content }
export async function PUT(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/instructions">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { content?: unknown };
  const content = typeof body.content === "string" ? body.content : "";

  const { id } = await context.params;
  return forward(
    `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/instructions`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, content }),
    },
    request,
  );
}

// DELETE /api/workspaces/{id}/instructions 清空指令
export async function DELETE(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/instructions">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  const url = new URL(`/internal/workspaces/${encodeURIComponent(id)}/instructions`, runtimeURL);
  url.searchParams.set("userId", userId);
  return forward(url, { method: "DELETE" }, request);
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
