import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// POST /api/workspaces/{id}/bind-local-root
// 方案 B：给已有工作区绑定本地目录（读写直连，无需重建工作区）。
// body: { localRoot: string, localRootPrefix?: string, mergeExisting?: boolean }
// 不传 localRootPrefix 时后端自动探测（"选文件夹上传"创建的工作区会多一层目录名，如 Data/）。
// mergeExisting 默认 false：不把旧副本的存量文件搬进本地目录；响应 unmergedFiles 回报未合并数。
export async function POST(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/bind-local-root">,
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
  const body = value as {
    localRoot?: unknown;
    localRootPrefix?: unknown;
    mergeExisting?: unknown;
  };
  if (typeof body.localRoot !== "string" || !body.localRoot.trim()) {
    return Response.json({ error: "localRoot is required" }, { status: 400 });
  }
  const localRootPrefix =
    typeof body.localRootPrefix === "string" ? body.localRootPrefix : undefined;

  return forward(
    `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/bind-local-root`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        userId,
        localRoot: body.localRoot.trim(),
        localRootPrefix,
        mergeExisting: body.mergeExisting === true,
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
