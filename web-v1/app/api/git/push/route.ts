import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// POST /api/git/push
// 把「当前会话沙箱里该工作区」的提交推送到绑定仓库。
// body: { sessionId: string, workspaceId: string, branch?: string }
// 凭据由服务端从凭据库取出后只在**后端**使用（经环境变量注入本地 git），不下发沙箱。
export async function POST(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { sessionId?: unknown; workspaceId?: unknown; branch?: unknown };
  if (typeof body.sessionId !== "string" || typeof body.workspaceId !== "string") {
    return Response.json(
      { error: "sessionId and workspaceId are required" },
      { status: 400 },
    );
  }
  const branch = typeof body.branch === "string" ? body.branch : "";

  return forward(
    `${runtimeURL}/internal/git/push`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        userId,
        sessionId: body.sessionId,
        workspaceId: body.workspaceId,
        branch,
      }),
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
