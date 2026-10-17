import { auth } from "@clerk/nextjs/server";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

async function forward(
  path: string,
  userId: string,
  method = "GET",
  requestId?: string,
): Promise<Response> {
  const url = new URL(path, runtimeURL);
  url.searchParams.set("userId", userId);
  const reqId = requestId ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      method,
      headers: { "X-Request-ID": reqId },
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
        "X-Request-ID": response.headers.get("X-Request-ID") ?? reqId,
      },
    });
  } catch {
    return Response.json({ error: "ETHOS runtime is unavailable" }, { status: 502 });
  }
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ requestId: string }> },
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { requestId } = await params;
  return forward(
    `/internal/gateway/runs/${encodeURIComponent(requestId)}`,
    userId,
    "GET",
    request.headers.get("X-Request-ID") ?? requestId,
  );
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ requestId: string }> },
) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });

  const { requestId } = await params;
  return forward(
    `/internal/gateway/runs/${encodeURIComponent(requestId)}/cancel`,
    userId,
    "POST",
    request.headers.get("X-Request-ID") ?? requestId,
  );
}
