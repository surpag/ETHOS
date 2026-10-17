// 普通文件仅在发送时上传；返回后端分配的 uploads/ 相对路径。
export async function uploadSandboxFile(sessionId: string, file: File): Promise<string> {
  if (file.size <= 0 || file.size > 50 * 1024 * 1024) throw new Error("文件必须非空且不超过 50MB");
  const body = new FormData(); body.append("file", file);
  const response = await fetch(`/api/sandbox/files/upload?sessionId=${encodeURIComponent(sessionId)}`, { method: "POST", body });
  if (!response.ok) throw new Error((await response.json().catch(() => null) as { message?: string } | null)?.message || "文件上传失败");
  const value = await response.json() as { path: string };
  return value.path;
}

// 上传工作区文件到 E2B Volume（P1）：文件名即 Volume 内相对路径（webkitRelativePath）。
export type WorkspaceUploadFile = { relPath: string; blob: Blob };

// 本次修改（2026-09-01）：
// ① 过滤：跳过 node_modules/.git/build/dist（大目录导致请求体超限）
// ② 分批：单批 ≤500 个文件
// ③ 方案 C：**直传后端 EDITH_RUNTIME_URL，绕开 Next.js BFF**——
//    BFF 用 request.body + duplex:"half" + Node fetch 流式转发大 multipart 不可靠（实测 RangeError/丢 part）。
//    浏览器直传后端需要后端开 CORS（backend main.py 已加 CORSMiddleware）。
const SKIP_DIR_SEGMENTS = new Set(["node_modules", ".git", "build", "dist"]);
const UPLOAD_BATCH_SIZE = 500;

// 浏览器直传后端地址：走 NEXT_PUBLIC 环境变量（默认本地开发后端）
const RUNTIME_URL =
  process.env.NEXT_PUBLIC_EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

export function filterWorkspaceFiles(files: WorkspaceUploadFile[]): WorkspaceUploadFile[] {
  return files.filter(({ relPath }) => {
    const segments = relPath.replace(/\\/g, "/").split("/");
    return !segments.some((segment) => SKIP_DIR_SEGMENTS.has(segment));
  });
}

export async function uploadWorkspaceFiles(
  workspaceId: string,
  files: WorkspaceUploadFile[],
  userId: string,
): Promise<{ uploaded: number; fileCount: number }> {
  const filtered = filterWorkspaceFiles(files);
  if (filtered.length === 0) throw new Error("没有可上传的文件（已过滤 node_modules 等目录）");

  let uploaded = 0;
  for (let offset = 0; offset < filtered.length; offset += UPLOAD_BATCH_SIZE) {
    const batch = filtered.slice(offset, offset + UPLOAD_BATCH_SIZE);
    const body = new FormData();
    for (const item of batch) {
      // 第二个参数是 filename，后端以此为 Volume 内相对路径（如 src/main.py）
      body.append("files", item.blob, item.relPath);
    }
    // 方案 C：直传后端（userId 由前端注入；后端 CORS 已放开）
    const response = await fetch(
      `${RUNTIME_URL}/internal/workspaces/${encodeURIComponent(workspaceId)}/files?userId=${encodeURIComponent(userId)}`,
      { method: "POST", body },
    );
    if (!response.ok) {
      const value = await response.json().catch(() => null) as { message?: string } | null;
      throw new Error(value?.message ?? "工作区文件上传失败");
    }
    const value = await response.json() as { uploaded: number; fileCount: number };
    uploaded += value.uploaded;
  }
  return { uploaded, fileCount: filtered.length };
}

