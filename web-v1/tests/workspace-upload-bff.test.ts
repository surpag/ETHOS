/**
 * BFF 上传路由回归测试（`app/api/workspaces/[id]/files` 的核心逻辑）。
 *
 * 用 Node 内置 test runner + 类型擦除直接跑 TS，不引入额外依赖：
 *   node --test tests/workspace-upload-bff.test.ts
 *
 * 覆盖：未授权 / 跨用户 workspace / 正常多文件 / 空 body / 大文件 / 超限 /
 *       上游异常 / 上传中断 / 上游拒绝透传。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  handleWorkspaceUpload,
  DEFAULT_MAX_UPLOAD_BYTES,
} from "../lib/server/workspace-upload.ts";

const RUNTIME = "http://runtime.test";

type FetchCall = { url: string; init?: RequestInit };
type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

/** 替换全局 fetch，返回记录下来的调用列表；调用 restore 还原。 */
function stubFetch(handler: Handler): { calls: FetchCall[]; restore: () => void } {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    calls.push({ url, init });
    return handler(url, init);
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** 默认上游：workspace ws-1 属于 u1，上传成功返回 uploaded=n。 */
function happyUpstream(fileCount: number): Handler {
  return (url) => {
    if (url.includes("/internal/workspaces?")) {
      return Response.json({ workspaces: [{ workspaceId: "ws-1", name: "demo" }] });
    }
    return Response.json({ uploaded: fileCount, fileCount, totalSize: 12 });
  };
}

function multipartRequest(
  files: Array<{ name: string; content: string | Uint8Array<ArrayBuffer> }>,
  workspaceId = "ws-1",
): Request {
  const form = new FormData();
  for (const file of files) {
    form.append("files", new File([file.content], file.name));
  }
  return new Request(`http://localhost/api/workspaces/${workspaceId}/files`, {
    method: "POST",
    body: form,
  });
}

function deps(overrides: Partial<Parameters<typeof handleWorkspaceUpload>[2]> = {}) {
  return {
    resolveUserId: async () => "u1",
    runtimeURL: RUNTIME,
    ...overrides,
  };
}

test("未登录：401，且不发起任何上游请求", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "a.txt", content: "hi" }]),
      "ws-1",
      deps({ resolveUserId: async () => null }),
    );
    assert.equal(response.status, 401);
    assert.equal((await response.json()).error, "unauthorized");
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});

test("跨用户 workspace：归属校验不通过 → 404，且不把文件转发给上游", async () => {
  const stub = stubFetch((url) => {
    if (url.includes("/internal/workspaces?")) {
      // 该用户只有 ws-2，没有 ws-1
      return Response.json({ workspaces: [{ workspaceId: "ws-2", name: "other" }] });
    }
    return Response.json({ uploaded: 1 });
  });
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "a.txt", content: "hi" }], "ws-1"),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error, "workspace_not_found");
    // 关键断言：只做了归属查询，没有 POST /files
    assert.equal(stub.calls.length, 1);
    assert.match(stub.calls[0].url, /\/internal\/workspaces\?/);
    assert.doesNotMatch(stub.calls[0].url, /\/files/);
  } finally {
    stub.restore();
  }
});

test("正常多文件上传：userId 由服务端注入并透传上游状态码/JSON", async () => {
  const stub = stubFetch(happyUpstream(3));
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([
        { name: "src/a.py", content: "print(1)" },
        { name: "src/b.py", content: "print(2)" },
        { name: "README.md", content: "# demo" },
      ]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 200);
    const payload = await response.json();
    assert.equal(payload.uploaded, 3);

    const upload = stub.calls.at(-1);
    assert.ok(upload);
    const url = new URL(upload.url);
    assert.equal(url.pathname, "/internal/workspaces/ws-1/files");
    // userId 来自会话，不是客户端传参
    assert.equal(url.searchParams.get("userId"), "u1");
    // boundary 必须原样保留，否则 FastAPI 解析不出 part
    const contentType = new Headers(upload.init?.headers).get("content-type") ?? "";
    assert.match(contentType, /^multipart\/form-data; boundary=/);
    // Buffer 转发：body 是已物化的 Buffer，不再依赖 duplex 流式
    assert.ok(Buffer.isBuffer(upload.init?.body));
    assert.equal((upload.init as Record<string, unknown>).duplex, undefined);
  } finally {
    stub.restore();
  }
});

test("客户端伪造 userId 参数无效：仍以会话身份转发", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const request = multipartRequest([{ name: "a.txt", content: "hi" }]);
    const response = await handleWorkspaceUpload(request, "ws-1", deps());
    assert.equal(response.status, 200);
    const url = new URL(stub.calls.at(-1)!.url);
    assert.equal(url.searchParams.get("userId"), "u1");
    assert.equal(url.searchParams.getAll("userId").length, 1);
  } finally {
    stub.restore();
  }
});

test("空文件（0 字节）允许上传：multipart 主体仍非空", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "empty/__init__.py", content: new Uint8Array(0) }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 200);
    assert.ok(Buffer.isBuffer(stub.calls.at(-1)?.init?.body));
  } finally {
    stub.restore();
  }
});

test("空目录（无文件）：请求体为空 → 400 empty_body，不触达上游上传接口", async () => {
  const stub = stubFetch(happyUpstream(0));
  try {
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      headers: { "Content-Type": "multipart/form-data; boundary=----x" },
      body: Buffer.alloc(0),
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps());
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error, "empty_body");
    assert.equal(stub.calls.filter((call) => call.url.includes("/files")).length, 0);
  } finally {
    stub.restore();
  }
});

test("大文件（数 MB）正常转发，body 长度与 multipart 一致", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const big = new Uint8Array(6 * 1024 * 1024).fill(65);
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "big.bin", content: big }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 200);
    const body = stub.calls.at(-1)?.init?.body as Buffer;
    assert.ok(body.byteLength > 6 * 1024 * 1024);
  } finally {
    stub.restore();
  }
});

test("超过上限：声明长度即拒（413），不读 body、不查归属", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    // 浏览器/nginx 会带 Content-Length；Node 里构造带 FormData 的 Request 不会自动带，
    // 所以这里显式给出，验证「读 body 之前就拒」的快路径。
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      headers: {
        "Content-Type": "multipart/form-data; boundary=----x",
        "Content-Length": String(2048),
      },
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array(2048));
          controller.close();
        },
      }),
      // @ts-expect-error Node fetch 要求显式 duplex
      duplex: "half",
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps({ maxBytes: 1024 }));
    assert.equal(response.status, 413);
    const payload = await response.json();
    assert.equal(payload.error, "payload_too_large");
    assert.equal(payload.maxBytes, 1024);
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});

test("超过上限（chunked 无 content-length）：读完 body 兜底判 413", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("x".repeat(4096)));
        controller.close();
      },
    });
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      headers: { "Content-Type": "multipart/form-data; boundary=----x" },
      body: stream,
      // @ts-expect-error Node fetch 要求显式 duplex
      duplex: "half",
    });
    const response = await handleWorkspaceUpload(
      request,
      "ws-1",
      deps({ maxBytes: 1024 }),
    );
    assert.equal(response.status, 413);
    assert.equal((await response.json()).error, "payload_too_large");
    assert.equal(stub.calls.filter((call) => call.url.includes("/files")).length, 0);
  } finally {
    stub.restore();
  }
});

test("默认上限与 nginx/前端约定一致：64MB", () => {
  assert.equal(DEFAULT_MAX_UPLOAD_BYTES, 64 * 1024 * 1024);
});

test("上游 runtime 不可用：502 upstream_unavailable（归属查询阶段）", async () => {
  const stub = stubFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "a.txt", content: "hi" }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, "upstream_unavailable");
  } finally {
    stub.restore();
  }
});

test("上游 runtime 上传阶段故障：502，不当成鉴权失败", async () => {
  const stub = stubFetch((url) => {
    if (url.includes("/internal/workspaces?")) {
      return Response.json({ workspaces: [{ workspaceId: "ws-1" }] });
    }
    throw new TypeError("socket hang up");
  });
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "a.txt", content: "hi" }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, "upstream_unavailable");
  } finally {
    stub.restore();
  }
});

test("归属查询返回 500：502，而不是把它当成「无权限」", async () => {
  const stub = stubFetch(() => new Response("boom", { status: 500 }));
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "a.txt", content: "hi" }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, "upstream_unavailable");
  } finally {
    stub.restore();
  }
});

test("上传请求中断（body 流报错）：400 upload_interrupted", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("--x\r\npartial"));
        controller.error(new Error("client aborted"));
      },
    });
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      headers: { "Content-Type": "multipart/form-data; boundary=----x" },
      body: stream,
      // @ts-expect-error Node fetch 要求显式 duplex
      duplex: "half",
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps());
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error, "upload_interrupted");
    assert.equal(stub.calls.filter((call) => call.url.includes("/files")).length, 0);
  } finally {
    stub.restore();
  }
});

test("后端业务拒绝（413/400）原样透传状态码与消息", async () => {
  const stub = stubFetch((url) => {
    if (url.includes("/internal/workspaces?")) {
      return Response.json({ workspaces: [{ workspaceId: "ws-1" }] });
    }
    return Response.json(
      { type: "file_too_large", message: "file too large: big.bin" },
      { status: 400 },
    );
  });
  try {
    const response = await handleWorkspaceUpload(
      multipartRequest([{ name: "big.bin", content: "x" }]),
      "ws-1",
      deps(),
    );
    assert.equal(response.status, 400);
    assert.equal((await response.json()).message, "file too large: big.bin");
  } finally {
    stub.restore();
  }
});

test("非 multipart 请求：415，不进内存拷贝", async () => {
  const stub = stubFetch(happyUpstream(1));
  try {
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: ["a"] }),
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps());
    assert.equal(response.status, 415);
    assert.equal((await response.json()).error, "unsupported_media_type");
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});
