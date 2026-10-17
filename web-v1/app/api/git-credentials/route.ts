import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/git-credentials
// 返回已连接凭据的 provider 列表（永不返回 token 本身）。
export async function GET(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  return forward(
    `${runtimeURL}/internal/git-credentials?userId=${encodeURIComponent(userId)}`,
    { method: "GET" },
    request,
  );
}

// PUT /api/git-credentials
// body: { provider?: string, token: string } —— 保存（或覆盖）Git 凭据。
// token 只在后端使用（注入 git 执行环境），不会下发到 Agent 沙箱。
export async function PUT(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid body" }, { status: 400 });
  }
  const body = value as { provider?: unknown; token?: unknown };
  if (typeof body.token !== "string" || !body.token.trim()) {
    return Response.json({ error: "token is required" }, { status: 400 });
  }
  const provider = typeof body.provider === "string" ? body.provider : "github";

  return forward(
    `${runtimeURL}/internal/git-credentials`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, provider, token: body.token }),
    },
    request,
  );
}

// DELETE /api/git-credentials?provider=github —— 断开某个 provider 的凭据。
export async function DELETE(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const provider = new URL(request.url).searchParams.get("provider") ?? "github";
  return forward(
    `${runtimeURL}/internal/git-credentials?userId=${encodeURIComponent(userId)}&provider=${encodeURIComponent(provider)}`,
    { method: "DELETE" },
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
