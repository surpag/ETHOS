/**
 * BFF 上传的端到端集成测试：真实 HTTP 上游（模拟 FastAPI 的 multipart 解析）。
 *
 *   node --test tests/workspace-upload-integration.test.ts
 *
 * 单测里 fetch 是桩，无法证明「Buffer 转发后 multipart 仍然合法」——而这正是
 * 本次改动的核心。这里起一个真的 node:http 服务，用 `formData()` 反向解析收到的
 * 请求体，确保 boundary、part 名、文件名、二进制内容全部完好。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { once } from "node:events";

import { handleWorkspaceUpload } from "../lib/server/workspace-upload.ts";

type Received = {
  method: string;
  url: string;
  contentType: string;
  files: Array<{ name: string; size: number; sha: string }>;
};

function hash(bytes: Uint8Array): string {
  let acc = 0;
  for (const byte of bytes) acc = (acc * 31 + byte) >>> 0;
  return acc.toString(16);
}

async function readBody(request: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

/**
 * 起一个最小的「runtime」：GET /internal/workspaces 返回属于 u1 的 ws-1，
 * POST /internal/workspaces/{id}/files 解析 multipart 并记录每个 part。
 */
async function startRuntime(options: { owned?: boolean } = {}): Promise<{
  runtimeURL: string;
  received: Received[];
  close: () => Promise<void>;
}> {
  const received: Received[] = [];
  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    if (req.method === "GET" && url.pathname === "/internal/workspaces") {
      const owned = options.owned ?? true;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          workspaces: owned ? [{ workspaceId: "ws-1", name: "demo" }] : [],
        }),
      );
      return;
    }
    if (req.method === "POST" && /^\/internal\/workspaces\/[^/]+\/files$/.test(url.pathname)) {
      const body = await readBody(req);
      const parsed = await new Response(body as unknown as BodyInit, {
        headers: { "Content-Type": req.headers["content-type"] ?? "" },
      }).formData();
      const files: Received["files"] = [];
      for (const value of parsed.getAll("files")) {
        if (value instanceof File) {
          const bytes = new Uint8Array(await value.arrayBuffer());
          files.push({ name: value.name, size: bytes.byteLength, sha: hash(bytes) });
        }
      }
      received.push({
        method: req.method,
        url: req.url ?? "",
        contentType: req.headers["content-type"] ?? "",
        files,
      });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ uploaded: files.length, fileCount: files.length }));
      return;
    }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ type: "not_found", message: "no route" }));
  });

  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("no port");
  return {
    runtimeURL: `http://127.0.0.1:${address.port}`,
    received,
    close: async () => {
      server.close();
      await once(server, "close");
    },
  };
}

function deps(runtimeURL: string) {
  return { resolveUserId: async () => "u1", runtimeURL };
}

test("真实上游：三个文件（含中文名/二进制/空文件）逐个完好抵达", async () => {
  const runtime = await startRuntime();
  try {
    const binary = new Uint8Array(1024);
    for (let index = 0; index < binary.length; index += 1) binary[index] = index % 256;

    const form = new FormData();
    form.append("files", new File(["print('你好')"], "src/你好.py"));
    form.append("files", new File([binary], "assets/data.bin"));
    form.append("files", new File([new Uint8Array(0)], "pkg/__init__.py"));
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      body: form,
    });

    const response = await handleWorkspaceUpload(request, "ws-1", deps(runtime.runtimeURL));
    assert.equal(response.status, 200);
    assert.equal((await response.json()).uploaded, 3);

    assert.equal(runtime.received.length, 1);
    const call = runtime.received[0];
    assert.equal(call.method, "POST");
    // userId 由 BFF 注入
    assert.match(call.url, /[?&]userId=u1(&|$)/);
    assert.match(call.contentType, /^multipart\/form-data; boundary=/);
    assert.deepEqual(
      call.files.map((file) => file.name),
      ["src/你好.py", "assets/data.bin", "pkg/__init__.py"],
    );
    assert.equal(call.files[1].size, 1024);
    assert.equal(call.files[1].sha, hash(binary));
    // 空文件不被丢弃（0 字节）
    assert.equal(call.files[2].size, 0);
  } finally {
    await runtime.close();
  }
});

test("真实上游：50 个文件一批仍然完整（BFF 不做 part 数量假设）", async () => {
  const runtime = await startRuntime();
  try {
    const form = new FormData();
    for (let index = 0; index < 50; index += 1) {
      form.append("files", new File([`content-${index}`], `batch/f${index}.txt`));
    }
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      body: form,
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps(runtime.runtimeURL));
    assert.equal(response.status, 200);
    assert.equal(runtime.received[0].files.length, 50);
    assert.equal(runtime.received[0].files[49].name, "batch/f49.txt");
  } finally {
    await runtime.close();
  }
});

test("真实上游：workspace 不属于该用户 → 404，上游收不到上传请求", async () => {
  const runtime = await startRuntime({ owned: false });
  try {
    const form = new FormData();
    form.append("files", new File(["x"], "a.txt"));
    const request = new Request("http://localhost/api/workspaces/ws-1/files", {
      method: "POST",
      body: form,
    });
    const response = await handleWorkspaceUpload(request, "ws-1", deps(runtime.runtimeURL));
    assert.equal(response.status, 404);
    assert.equal(runtime.received.length, 0);
  } finally {
    await runtime.close();
  }
});

test("真实上游：连不上 → 502", async () => {
  // 先起再关，拿到一个确定没人监听的端口
  const runtime = await startRuntime();
  const deadURL = runtime.runtimeURL;
  await runtime.close();

  const form = new FormData();
  form.append("files", new File(["x"], "a.txt"));
  const request = new Request("http://localhost/api/workspaces/ws-1/files", {
    method: "POST",
    body: form,
  });
  const response = await handleWorkspaceUpload(request, "ws-1", deps(deadURL));
  assert.equal(response.status, 502);
  assert.equal((await response.json()).error, "upstream_unavailable");
});
