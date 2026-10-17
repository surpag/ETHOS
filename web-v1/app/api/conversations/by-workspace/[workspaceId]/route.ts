import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

// GET /api/conversations/by-workspace/[workspaceId]
// 返回该工作区当前的活跃会话（一个工作区只允许一个会话）。
// sessionId 为空 = 该工作区还没有会话，可以新建。
//
// 参数类型直接写出来而不是用生成的 RouteContext<...>：新路由在 Next 重新生成
// `.next/types/routes.d.ts` 之前不在这份联合类型里，会让 `tsc --noEmit` 报错。
export async function GET(
  _request: Request,
  context: { params: Promise<{ workspaceId: string }> },
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { workspaceId } = await context.params;
  const url = new URL(
    `/internal/conversations/by-workspace/${encodeURIComponent(workspaceId)}`,
    runtimeURL,
  );
  url.searchParams.set("userId", userId);
  try {
    const response = await fetch(url, { cache: "no-store" });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}
