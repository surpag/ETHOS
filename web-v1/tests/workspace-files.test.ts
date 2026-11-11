/**
 * 前端上传链路回归测试（`lib/sandbox/files.ts`）。
 *
 *   node --test tests/workspace-files.test.ts
 *
 * 覆盖：过滤 / 分批（数量 + 体积）/ 空目录 / 空文件 / 大文件 / 超限 /
 *       请求目标为 BFF 而非 runtime / 失败文案映射。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_FILE_BYTES,
  UPLOAD_BATCH_BYTES,
  UPLOAD_BATCH_SIZE,
  filterWorkspaceFiles,
  planUploadBatches,
  uploadWorkspaceFiles,
  type WorkspaceUploadFile,
} from "../lib/sandbox/files.ts";

/**
 * 只报告 size、不真正分配内存的 Blob（分批逻辑只读 `blob.size`）。
 * 继承 Blob 而非伪造对象，否则 FormData.append 会拒收。
 */
class SizedBlob extends Blob {
  private readonly reportedSize: number;

  constructor(size: number) {
    super([]);
    this.reportedSize = size;
  }

  override get size(): number {
    return this.reportedSize;
  }
}

function fakeFile(relPath: string, size: number): WorkspaceUploadFile {
  return { relPath, blob: new SizedBlob(size) };
}

function realFile(relPath: string, content: string): WorkspaceUploadFile {
  return { relPath, blob: new File([content], relPath.split("/").pop() ?? relPath) };
}

type FetchCall = { url: string; init?: RequestInit };

function stubFetch(handler: (url: string, init?: RequestInit) => Response): {
  calls: FetchCall[];
  restore: () => void;
} {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
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

test("批次配置：50 个文件 / 40MB", () => {
  assert.equal(UPLOAD_BATCH_SIZE, 50);
  assert.equal(UPLOAD_BATCH_BYTES, 40 * 1024 * 1024);
  assert.equal(MAX_FILE_BYTES, 50 * 1024 * 1024);
});

test("过滤 node_modules/.git/build/dist，保留其余", () => {
  const files = [
    fakeFile("src/main.py", 10),
    fakeFile("node_modules/react/index.js", 10),
    fakeFile(".git/HEAD", 10),
    fakeFile("dist/out.js", 10),
    fakeFile("build/x.o", 10),
    fakeFile("docs/readme.md", 10),
  ];
  const kept = filterWorkspaceFiles(files).map((file) => file.relPath);
  assert.deepEqual(kept, ["src/main.py", "docs/readme.md"]);
});

test("Windows 反斜杠路径同样能过滤", () => {
  const kept = filterWorkspaceFiles([
    fakeFile("src\\main.py", 10),
    fakeFile("node_modules\\x\\y.js", 10),
  ]);
  assert.deepEqual(kept.map((file) => file.relPath), ["src\\main.py"]);
});

test("分批：按 50 个切分", () => {
  const files = Array.from({ length: 120 }, (_, index) => fakeFile(`f${index}.txt`, 10));
  const batches = planUploadBatches(files);
  assert.equal(batches.length, 3);
  assert.deepEqual(
    batches.map((batch) => batch.length),
    [50, 50, 20],
  );
});

test("分批：按 40MB 体积切分（数量没到也要切）", () => {
  const oneMb = 1024 * 1024;
  const files = Array.from({ length: 50 }, (_, index) => fakeFile(`f${index}.bin`, oneMb));
  const batches = planUploadBatches(files);
  // 40 个 1MB 之后再加就超 40MB → 40 + 10
  assert.deepEqual(
    batches.map((batch) => batch.length),
    [40, 10],
  );
  for (const batch of batches) {
    const bytes = batch.reduce((sum, file) => sum + file.blob.size, 0);
    assert.ok(bytes <= UPLOAD_BATCH_BYTES, `batch 体积 ${bytes} 超过上限`);
  }
});

test("分批：单个 50MB 文件独占一批（不产生空批）", () => {
  const files = [fakeFile("big.bin", 50 * 1024 * 1024), fakeFile("small.txt", 10)];
  const batches = planUploadBatches(files);
  assert.equal(batches.length, 2);
  assert.deepEqual(batches.map((batch) => batch.map((f) => f.relPath)), [
    ["big.bin"],
    ["small.txt"],
  ]);
});

test("分批：空输入返回空数组", () => {
  assert.deepEqual(planUploadBatches([]), []);
});

test("空目录（没有文件）：uploadWorkspaceFiles 直接报错，不发请求", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 0 }));
  try {
    await assert.rejects(
      () => uploadWorkspaceFiles("ws-1", []),
      /没有可上传的文件/,
    );
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});

test("只有被过滤目录：同样视为空目录", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 0 }));
  try {
    await assert.rejects(
      () => uploadWorkspaceFiles("ws-1", [fakeFile("node_modules/a.js", 5)]),
      /没有可上传的文件/,
    );
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});

test("正常多文件上传：请求打到 BFF，不再直连 runtime，也不带 userId", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 2, fileCount: 2 }));
  try {
    const result = await uploadWorkspaceFiles("ws-1", [
      realFile("src/a.py", "print(1)"),
      realFile("src/b.py", "print(2)"),
    ]);
    assert.deepEqual(result, { uploaded: 2, fileCount: 2, batches: 1 });
    const call = stub.calls[0];
    assert.equal(call.url, "/api/workspaces/ws-1/files");
    assert.doesNotMatch(call.url, /^https?:/);
    assert.doesNotMatch(call.url, /userId/);
    assert.equal(call.init?.method, "POST");
    assert.ok(call.init?.body instanceof FormData);
  } finally {
    stub.restore();
  }
});

test("空文件（0 字节）照常上传，不被过滤", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 1 }));
  try {
    const result = await uploadWorkspaceFiles("ws-1", [realFile("pkg/__init__.py", "")]);
    assert.equal(result.uploaded, 1);
    const form = stub.calls[0].init?.body as FormData;
    const entry = form.get("files") as File;
    assert.equal(entry.size, 0);
    assert.equal(entry.name, "pkg/__init__.py");
  } finally {
    stub.restore();
  }
});

test("大文件（>40MB）单独成批上传", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 1 }));
  try {
    const result = await uploadWorkspaceFiles(
      "ws-1",
      [fakeFile("big.bin", 45 * 1024 * 1024), fakeFile("a.txt", 10)],
    );
    assert.equal(result.batches, 2);
    assert.equal(stub.calls.length, 2);
  } finally {
    stub.restore();
  }
});

test("超过单文件上限：本地即拒，不浪费一次注定失败的请求", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 1 }));
  try {
    await assert.rejects(
      () => uploadWorkspaceFiles("ws-1", [fakeFile("huge.bin", MAX_FILE_BYTES + 1)]),
      /huge\.bin 超过单文件 50MB 上限/,
    );
    assert.equal(stub.calls.length, 0);
  } finally {
    stub.restore();
  }
});

test("批次失败即中止，后续批次不再上传（避免半成品继续堆积）", async () => {
  let calls = 0;
  const stub = stubFetch(() => {
    calls += 1;
    return calls === 1
      ? Response.json({ uploaded: 40 })
      : Response.json({ error: "payload_too_large", message: "单次上传体积超过上限" }, { status: 413 });
  });
  try {
    const files = Array.from({ length: 60 }, (_, index) => fakeFile(`f${index}.bin`, 1024 * 1024));
    await assert.rejects(() => uploadWorkspaceFiles("ws-1", files), /单次上传体积超过上限/);
    assert.equal(stub.calls.length, 2);
  } finally {
    stub.restore();
  }
});

test("失败文案按状态码分类：401 / 404 / 413 都能看懂", async () => {
  const cases: Array<[number, RegExp]> = [
    [401, /登录已过期/],
    [404, /工作区不存在或无权访问/],
    [413, /单次上传体积超过限制/],
  ];
  for (const [status, pattern] of cases) {
    const stub = stubFetch(() => new Response("{}", { status }));
    try {
      await assert.rejects(
        () => uploadWorkspaceFiles("ws-1", [realFile("a.txt", "x")]),
        pattern,
      );
    } finally {
      stub.restore();
    }
  }
});

test("workspaceId 做 URL 编码，避免路径注入", async () => {
  const stub = stubFetch(() => Response.json({ uploaded: 1 }));
  try {
    await uploadWorkspaceFiles("ws/../../etc", [realFile("a.txt", "x")]);
    assert.equal(stub.calls[0].url, "/api/workspaces/ws%2F..%2F..%2Fetc/files");
  } finally {
    stub.restore();
  }
});
