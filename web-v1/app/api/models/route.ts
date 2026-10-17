import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

export async function GET(request: Request) {
  const { userId } = await auth();
  if (!userId) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const requestId =
    request.headers.get("X-Request-ID") ?? crypto.randomUUID();

  // userId 必带：目录需要合并该用户的自定义供应商与模型；缺省时后端只返回内置目录。
  let response: Response;
  try {
    response = await fetch(
      `${runtimeURL}/internal/models?userId=${encodeURIComponent(userId)}`,
      {
        headers: { "X-Request-ID": requestId },
        cache: "no-store",
      },
    );
  } catch {
    return Response.json(
      { error: "ETHOS runtime is unavailable" },
      { status: 502 },
    );
  }

  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store",
      "X-Request-ID": response.headers.get("X-Request-ID") ?? requestId,
    },
  });
}
