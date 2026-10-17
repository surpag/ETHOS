import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// PUT /api/workspaces/{id}/allowed-domains
// 沙箱出口白名单（存于 workspace metadata.allowedDomains）。
// body: { domains: string[] } —— 空数组 = 清除白名单（恢复全放通）。
// 生效时机：下一次沙箱连接（后端每次连接都会核对并下发策略）。
export async function PUT(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/allowed-domains">,
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
  const body = value as { domains?: unknown };
  if (!Array.isArray(body.domains)) {
    return Response.json({ error: "domains must be an array" }, { status: 400 });
  }
  const domains = body.domains
    .filter((d): d is string => typeof d === "string")
    .map((d) => d.trim())
    .filter(Boolean);

  return forward(
    `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/allowed-domains`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ userId, domains }),
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
