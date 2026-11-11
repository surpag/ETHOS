/**
 * 工作区文件上传的 BFF 实现（服务端专用，禁止在客户端组件里 import）。
 *
 * 链路：
 *   browser --POST /api/workspaces/{id}/files--> Next.js BFF --POST
 *   /internal/workspaces/{id}/files?userId=--> ETHOS runtime
 *
 * 为什么不再让浏览器直连 runtime（原「方案 C」）：
 *   `NEXT_PUBLIC_EDITH_RUNTIME_URL` 是**构建期内联**的，容器部署下它指向的是
 *   构建机/开发机的 127.0.0.1，浏览器会去连自己的电脑 → `Failed to fetch`，
 *   而且后端日志里看不到任何请求。把 `/internal/*` 暴露给公网本身也不该做。
 *
 * 为什么用 Buffer 而不是 `request.body + duplex: "half"` 流式转发：
 *   Node 的 fetch 对未定长 multipart 流式请求不可靠（实测 RangeError / 丢 part），
 *   且失败时错误信息与「上游拒绝」无法区分。整段读入内存后转发行为稳定，
 *   代价是单请求内存上限 = MAX_UPLOAD_BYTES，由调用方（前端分批）保证不越界。
 *
 * 安全边界：
 *   - userId 只由服务端会话（Clerk）解析，永不采信客户端传参。
 *   - 转发前先用 `/internal/workspaces?userId=` 校验 workspace 归属，
 *     既避免为「别人的 workspace id」读入整包请求体，也让越权返回 404 而非 403
 *     （不泄露 workspace 是否存在）。
 *   - `/internal/*` 仍然只在容器网络内可达，本模块是唯一出网口，nginx 不新增反代。
 */

/** 单次上传请求体的硬上限。前端分批（≤50MB/批）后不会触及。 */
export const DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024;

/** 归属校验的超时：一次 SQLite 查询，正常 <50ms；超时即视为上游不可用。 */
const OWNERSHIP_TIMEOUT_MS = 5_000;

export type WorkspaceUploadDeps = {
  /**
   * 解析当前登录用户。返回 null 表示未登录。
   * 用回调注入而非直接 import Clerk，使核心逻辑可在 Node 测试里跑。
   */
  resolveUserId: () => Promise<string | null>;
  /** 覆盖 runtime 地址（默认读 EDITH_RUNTIME_URL）。 */
  runtimeURL?: string;
  /** 覆盖请求体上限（测试用）。 */
  maxBytes?: number;
};

function runtimeBase(): string {
  return process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";
}

function jsonError(
  status: number,
  code: string,
  message: string,
  extra?: Record<string, unknown>,
): Response {
  return Response.json({ error: code, message, ...extra }, { status });
}

/**
 * 校验 workspace 归属于 userId。
 *
 * runtime 内部的 `/internal/workspaces` 只按 userId 过滤，因此「列表里有这个 id」
 * 等价于「这个 workspace 属于该用户」。查询失败（5xx/网络/超时）抛错——
 * 此时**不能**当作「无权限」，那是上游故障而不是鉴权结论。
 */
async function workspaceOwnedBy(
  userId: string,
  workspaceId: string,
  runtimeURL: string,
  requestId: string,
): Promise<boolean> {
  const url = new URL("/internal/workspaces", runtimeURL);
  url.searchParams.set("userId", userId);
  const response = await fetch(url, {
    headers: { "X-Request-ID": requestId },
    cache: "no-store",
    signal: AbortSignal.timeout(OWNERSHIP_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`workspace lookup failed: ${response.status}`);
  }
  const payload = (await response.json()) as {
    workspaces?: Array<{ workspaceId?: string; workspace_id?: string }>;
  };
  const list = payload.workspaces ?? [];
  return list.some(
    (item) => item.workspaceId === workspaceId || item.workspace_id === workspaceId,
  );
}

/**
 * 处理 POST /api/workspaces/{id}/files。
 *
 * 返回的 Response 直接回给浏览器：状态码与上游保持一致，前端据 401/404/413 区分文案。
 */
export async function handleWorkspaceUpload(
  request: Request,
  workspaceId: string,
  deps: WorkspaceUploadDeps,
): Promise<Response> {
  const userId = await deps.resolveUserId();
  if (!userId) {
    return jsonError(401, "unauthorized", "未登录或登录已过期");
  }

  const id = (workspaceId ?? "").trim();
  if (!id) {
    return jsonError(400, "invalid_workspace", "workspace id is required");
  }

  const runtimeURL = deps.runtimeURL ?? runtimeBase();
  const maxBytes = deps.maxBytes ?? DEFAULT_MAX_UPLOAD_BYTES;
  const requestId = request.headers.get("x-request-id") || crypto.randomUUID();

  const contentType = request.headers.get("content-type") ?? "";
  if (!/^multipart\/form-data/i.test(contentType)) {
    return jsonError(
      415,
      "unsupported_media_type",
      "请求必须是 multipart/form-data（含 boundary）",
    );
  }

  // 先看声明的长度：不合规的请求连内存都不该占。
  const declaredLength = Number(request.headers.get("content-length") ?? "NaN");
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    return jsonError(413, "payload_too_large", "单次上传体积超过上限", { maxBytes });
  }

  // 归属校验必须早于读请求体：越权 id 不该换来一次完整的内存拷贝。
  let owned: boolean;
  try {
    owned = await workspaceOwnedBy(userId, id, runtimeURL, requestId);
  } catch {
    return jsonError(502, "upstream_unavailable", "ETHOS runtime 暂不可用，请稍后重试");
  }
  if (!owned) {
    return jsonError(404, "workspace_not_found", "工作区不存在或无权访问");
  }

  let body: Buffer;
  try {
    body = Buffer.from(await request.arrayBuffer());
  } catch {
    // 客户端中断 / 连接被 nginx 截断：不是服务端错误，也不该重试同一请求。
    return jsonError(400, "upload_interrupted", "上传请求被中断");
  }
  if (body.byteLength === 0) {
    return jsonError(400, "empty_body", "请求体为空");
  }
  // 没有 content-length（chunked）时只能读完后兜底判断。
  if (body.byteLength > maxBytes) {
    return jsonError(413, "payload_too_large", "单次上传体积超过上限", { maxBytes });
  }

  const target = new URL(
    `/internal/workspaces/${encodeURIComponent(id)}/files`,
    runtimeURL,
  );
  // userId 由服务端注入；即使 URL 上带了 userId，也以这里为准（覆盖不了）
  target.searchParams.set("userId", userId);

  try {
    const response = await fetch(target, {
      method: "POST",
      // 原样带上 boundary；不要自己拼 Content-Type
      headers: { "Content-Type": contentType, "X-Request-ID": requestId },
      // Buffer 是 Uint8Array 子类，undici 直接接受；TS 5.7+ 把 Uint8Array 的
      // buffer 参数化为 ArrayBufferLike，与 DOM 的 BodyInit 不兼容，故断言。
      body: body as unknown as BodyInit,
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
        "X-Request-ID": response.headers.get("x-request-id") ?? requestId,
      },
    });
  } catch {
    return jsonError(502, "upstream_unavailable", "ETHOS runtime 暂不可用，请稍后重试");
  }
}
