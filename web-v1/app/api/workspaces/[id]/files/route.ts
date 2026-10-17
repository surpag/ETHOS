import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// POST /api/workspaces/{id}/files 上传工作区文件（multipart 原样转发）
export async function POST(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/files">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { id } = await context.params;
  const requestId = request.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    // FormData 由浏览器自动带 boundary，BFF 不得手动设置 Content-Type
    const response = await fetch(
      `${runtimeURL}/internal/workspaces/${encodeURIComponent(id)}/files?userId=${encodeURIComponent(userId)}`,
      {
        method: "POST",
        body: request.body,
        headers: {
          "X-Request-ID": requestId,
          // 透传浏览器传来的 multipart Content-Type（含 boundary）
          ...(request.headers.get("content-type")
            ? { "Content-Type": request.headers.get("content-type") as string }
            : {}),
        },
        cache: "no-store",
        // Node 18 fetch 流式请求需要 duplex: "half"（RequestInit 类型未收录，断言绕过）
        duplex: "half",
      } as RequestInit & { duplex: "half" },
    );
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
