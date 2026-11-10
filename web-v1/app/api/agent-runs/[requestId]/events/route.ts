import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/agent-runs/{requestId}/events?after=N
//
// 断流重连的 BFF 入口：把「已收到的事件序号」带给 runtime，先补发错过的
// 事件再转实时。为什么不用浏览器 EventSource：它没法带 Clerk 认证，也没法
// 让 BFF 从会话里取 userId（userId 绝不能由浏览器给）。所以还是 fetch + 手写
// 的 SSE 读取器。
export async function GET(
  request: Request,
  { params }: { params: Promise<{ requestId: string }> },
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { requestId } = await params;
  const after = new URL(request.url).searchParams.get("after") ?? "0";

  const url = new URL(
    `/internal/gateway/runs/${encodeURIComponent(requestId)}/events`,
    runtimeURL,
  );
  url.searchParams.set("userId", userId);
  url.searchParams.set("after", /^\d+$/.test(after) ? after : "0");

  let response: Response;
  try {
    response = await fetch(url, {
      headers: { "X-Request-ID": requestId },
      cache: "no-store",
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }

  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") ?? "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
