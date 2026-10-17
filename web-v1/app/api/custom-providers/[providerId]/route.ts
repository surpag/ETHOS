import { auth } from "@clerk/nextjs/server";

import { forwardRuntime } from "@/lib/models/runtime";

// DELETE /api/custom-providers/[providerId]
// 连带删除该供应商下的模型与 Key（后端一次事务完成，前端不需要二次清理）。
export async function DELETE(
  request: Request,
  context: RouteContext<"/api/custom-providers/[providerId]">,
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { providerId } = await context.params;
  return forwardRuntime(
    `/internal/custom-providers/${encodeURIComponent(providerId)}?userId=${encodeURIComponent(userId)}`,
    { method: "DELETE" },
    request,
  );
}
