// BFF → ETHOS runtime 的转发工具（自定义模型相关路由共用）。
// 与各 route 里既有的 forward 保持一致的行为：注入 X-Request-ID、禁用缓存、
// 原样透传状态码与响应体（后端的 {"type","message"} 错误体可以直接展示）。

export const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

export async function forwardRuntime(
  path: string,
  init: RequestInit = {},
  request?: Request,
): Promise<Response> {
  const requestId = request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(`${runtimeURL}${path}`, {
      ...init,
      headers: { ...init.headers, "X-Request-ID": requestId },
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

export async function readJSON(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const value = await request.json();
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}
