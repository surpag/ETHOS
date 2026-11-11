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

/**
 * 上传链路（2026-11-11 调整）：
 *
 *   browser --POST /api/workspaces/{id}/files--> Next.js BFF --> ETHOS runtime
 *
 * 旧实现（方案 C）让浏览器**直连** `NEXT_PUBLIC_EDITH_RUNTIME_URL`。该变量是
 * 构建期内联，容器部署时会被固化成构建机的 `127.0.0.1:8080`，浏览器于是去连
 * 自己的电脑 → `Failed to fetch`（后端日志里一条请求都没有）。同时把 runtime 的
 * `/internal/*` 暴露到公网换取「省一次转发」，不值得。现在统一走 BFF：
 * 鉴权、workspace 归属校验、请求体上限都在服务端做，前端不参与任何权限判断。
 */

/** 跳过的大目录：node_modules 之类会把单批请求体顶爆，且模型也不需要。 */
const SKIP_DIR_SEGMENTS = new Set(["node_modules", ".git", "build", "dist"]);

/** 单文件上限，与后端 `MAX_FILE_BYTES` 对齐。 */
export const MAX_FILE_BYTES = 50 * 1024 * 1024;

/**
 * 单批文件数（原 500）。
 * 50 的上限同时被 multipart 解析器（每 part 的 header 都会进 form-data 解析）、
 * BFF 内存拷贝和 nginx 请求体限制约束；500 时单批可能达到数百 MB。
 */
export const UPLOAD_BATCH_SIZE = 50;

/**
 * 单批总字节上限（40MB）。
 * 只按数量分批不够：50 个 5MB 文件 = 250MB，仍会越过 BFF/nginx 限制。
 * 单批因此按「数量 或 体积」先到者切分；单个文件超上限时独占一批。
 */
export const UPLOAD_BATCH_BYTES = 40 * 1024 * 1024;

export function filterWorkspaceFiles(files: WorkspaceUploadFile[]): WorkspaceUploadFile[] {
  return files.filter(({ relPath }) => {
    const segments = relPath.replace(/\\/g, "/").split("/");
    return !segments.some((segment) => SKIP_DIR_SEGMENTS.has(segment));
  });
}

/**
 * 把文件切成上传批次（纯函数，便于单测）。
 *
 * 规则：累计到 50 个 或 累计体积超过 40MB 就开新批；每批至少 1 个文件
 * （否则单个 50MB 文件会切出空批）。
 */
export function planUploadBatches(files: WorkspaceUploadFile[]): WorkspaceUploadFile[][] {
  const batches: WorkspaceUploadFile[][] = [];
  let current: WorkspaceUploadFile[] = [];
  let bytes = 0;
  for (const file of files) {
    const wouldExceed =
      current.length >= UPLOAD_BATCH_SIZE || bytes + file.blob.size > UPLOAD_BATCH_BYTES;
    if (current.length > 0 && wouldExceed) {
      batches.push(current);
      current = [];
      bytes = 0;
    }
    current.push(file);
    bytes += file.blob.size;
  }
  if (current.length > 0) batches.push(current);
  return batches;
}

/** 超出上限的文件不让它进网络：整批都会被后端 400 拒掉，先给出具体文件名。 */
export function assertFilesWithinLimit(files: WorkspaceUploadFile[]): void {
  const oversized = files.find((file) => file.blob.size > MAX_FILE_BYTES);
  if (oversized) {
    throw new Error(`文件 ${oversized.relPath} 超过单文件 50MB 上限`);
  }
}

function uploadEndpoint(workspaceId: string): string {
  return `/api/workspaces/${encodeURIComponent(workspaceId)}/files`;
}

async function readFailureMessage(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => null)) as
    | { message?: string; error?: string }
    | null;
  if (response.status === 401) return "登录已过期，请重新登录后重试";
  if (response.status === 404) return body?.message ?? "工作区不存在或无权访问";
  if (response.status === 413) return body?.message ?? "单次上传体积超过限制，请分批上传";
  return body?.message ?? body?.error ?? fallback;
}

export async function uploadWorkspaceFiles(
  workspaceId: string,
  files: WorkspaceUploadFile[],
): Promise<{ uploaded: number; fileCount: number; batches: number }> {
  const filtered = filterWorkspaceFiles(files);
  if (filtered.length === 0) throw new Error("没有可上传的文件（已过滤 node_modules 等目录）");
  assertFilesWithinLimit(filtered);

  const batches = planUploadBatches(filtered);
  let uploaded = 0;
  for (const batch of batches) {
    const body = new FormData();
    for (const item of batch) {
      // 第二个参数是 filename，后端以此为 Volume 内相对路径（如 src/main.py）
      body.append("files", item.blob, item.relPath);
    }
    // 不再传 userId：身份由 BFF 从 Clerk 会话解析
    const response = await fetch(uploadEndpoint(workspaceId), {
      method: "POST",
      body,
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error(await readFailureMessage(response, "工作区文件上传失败"));
    }
    const value = (await response.json()) as { uploaded: number };
    uploaded += value.uploaded;
  }
  return { uploaded, fileCount: filtered.length, batches: batches.length };
}
