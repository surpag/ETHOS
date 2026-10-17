import { auth } from "@clerk/nextjs/server";

import { parseSaveUserSettingsRequest } from "@/lib/userconfig/request";

const runtimeURL = process.env.EDITH_RUNTIME_URL ?? "http://127.0.0.1:8080";

export async function GET(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });
  const url = new URL("/internal/user-settings", runtimeURL);
  url.searchParams.set("userId", userId);
  return forward(url, { method: "GET" }, request);
}

export async function PUT(request: Request) {
  const { userId } = await auth();
  if (!userId) return Response.json({ error: "Unauthorized" }, { status: 401 });
  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return Response.json({ error: "Invalid settings" }, { status: 400 });
  }
  const settings = parseSaveUserSettingsRequest(value);
  if (!settings) return Response.json({ error: "Invalid settings" }, { status: 400 });
  return forward(
    `${runtimeURL}/internal/user-settings`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...settings, userId }),
    },
    request,
  );
}

async function forward(url: string | URL, init?: RequestInit, request?: Request) {
  const requestId =
    request?.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const response = await fetch(url, {
      ...init,
      headers: { ...init?.headers, "X-Request-ID": requestId },
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
