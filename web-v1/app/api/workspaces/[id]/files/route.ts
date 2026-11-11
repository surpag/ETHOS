import { auth } from "@clerk/nextjs/server";

import { handleWorkspaceUpload } from "@/lib/server/workspace-upload";

/**
 * POST /api/workspaces/{id}/files —— 上传工作区文件。
 *
 * 鉴权/归属校验/转发规则全部在 `lib/server/workspace-upload.ts`，
 * 这里只负责把 Clerk 会话接进去（保持可测试性，核心逻辑不依赖 Clerk）。
 */
export async function POST(
  request: Request,
  context: RouteContext<"/api/workspaces/[id]/files">,
) {
  const { id } = await context.params;
  return handleWorkspaceUpload(request, id, {
    resolveUserId: async () => (await auth()).userId,
  });
}
