// 一个工作区只允许一个会话（后端用 session_meta 的部分唯一索引保证）。
// 前端在「点新会话 / 进入工作区」前先问这里，已有就直接打开，而不是造第二个沙箱
// 去覆盖同一份目录镜像。
export async function fetchActiveSessionID(workspaceId: string): Promise<string> {
  const id = workspaceId.trim();
  if (!id) return "";
  try {
    const response = await fetch(
      `/api/conversations/by-workspace/${encodeURIComponent(id)}`,
    );
    if (!response.ok) return "";
    const body = (await response.json()) as { sessionId?: string };
    return body.sessionId ?? "";
  } catch {
    return "";
  }
}
