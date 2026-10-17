import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// PUT /api/workspaces/{id}/git-remote
// 绑定 / 解绑工作区的 Git 远端仓库地址（存于 metadata.gitRemoteUrl）。
// body: { remoteUrl: string } —— 空串 = 解绑；非空需为公网 https/ssh 地址。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/git-remote">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { remoteUrl?: unknown };
  const remoteUrl = typeof body.remoteUrl === "string" ? body.remoteUrl.trim() : "";

  return forward(
    `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/git-remote`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, remoteUrl }),
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
