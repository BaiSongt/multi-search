import assert from "node:assert/strict";
import { createServer } from "node:http";
import { after, before, test } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { MultiSearchClient, MultiSearchError } from "./multisearch.ts";

const jobId = "12345678-0000-0000-0000-abcdef123456";
const token = "test-sdk-key";
const traceId = "a".repeat(32);
const received = [];
const foreignReceived = [];
let foreignUrl;
let base;

const foreign = createServer((request, response) => {
  foreignReceived.push(request.headers);
  response.writeHead(200).end("{}");
});

const api = createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null;
  const path = request.url;
  received.push({ path, method: request.method, authorization: request.headers.authorization, body });
  if (body?.query === "redirect") {
    response.writeHead(307, { Location: `${foreignUrl}/collect` }).end();
    return;
  }
  if (body?.query === "timeout") await sleep(150);
  if (body?.query === "oversize") {
    response.writeHead(200);
    response.write("x".repeat(200));
    response.end("y".repeat(200));
    return;
  }
  const headers = {
    "Content-Type": "application/json",
    "X-Trace-ID": traceId,
  };
  if (body?.query === "throttle") {
    response.writeHead(429, { ...headers, "Retry-After": "2" }).end(JSON.stringify({
      request_id: "from-body", code: "RATE_LIMITED",
      message: `Unsafe echoed content ${token}`, retryable: true,
    }));
    return;
  }
  headers["X-Request-ID"] = "from-header";
  if (path.endsWith("/capabilities")) {
    response.writeHead(200, headers).end(JSON.stringify({ modes: { quick: "available" } }));
  } else if (path.endsWith("/fetch") || path.endsWith("/research") || body?.mode === "deep") {
    response.writeHead(202, headers).end(JSON.stringify({ job_id: jobId, status_url: foreignUrl, status: "queued" }));
  } else if (path.includes("/jobs/")) {
    response.writeHead(200, headers).end(JSON.stringify({
      job_id: jobId, status: path.endsWith("/cancel") ? "cancelled" : "running",
    }));
  } else {
    response.writeHead(200, headers).end(JSON.stringify({ results: [] }));
  }
});

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    resolve(`http://127.0.0.1:${server.address().port}`);
  }));
}

before(async () => {
  [base, foreignUrl] = await Promise.all([listen(api), listen(foreign)]);
});

after(async () => {
  await Promise.all([api, foreign].map((server) => new Promise((resolve) => {
    server.close(resolve);
    server.closeAllConnections();
  })));
});

test("fixed API paths, idempotency, tracing and unauthenticated capability discovery", async () => {
  const client = new MultiSearchClient(`${base}/proxy/api/v1/`, { apiKey: token });
  assert.deepEqual((await client.capabilities()).body.modes, { quick: "available" });
  assert.deepEqual(received.at(-1), {
    path: "/proxy/api/v1/capabilities", method: "GET", authorization: undefined, body: null,
  });
  const search = await client.search({ query: "moon" }, { requestId: "caller", idempotencyKey: "id-001" });
  assert.deepEqual([search.status, search.requestId, search.traceId], [200, "from-header", traceId]);
  assert.deepEqual(received.at(-1), {
    path: "/proxy/api/v1/search", method: "POST", authorization: `Bearer ${token}`,
    body: { query: "moon", request_id: "caller", idempotency_key: "id-001" },
  });
  const fetched = await client.fetch({ url: "https://example.org" });
  assert.equal(fetched.status, 202);
  assert.equal((await client.research({ query: "moon", idempotency_key: "research-001" })).status, 202);
  assert.equal((await client.search({ query: "moon", mode: "deep" })).status, 202);
  assert.equal((await client.getJob(fetched.body.job_id)).body.status, "running");
  assert.equal((await client.cancelJob(jobId)).body.status, "cancelled");
  assert.equal(received.at(-1).path, `/proxy/api/v1/jobs/${jobId}/cancel`);
  assert.deepEqual(foreignReceived, []);
});

test("blocks redirects before the receiving origin sees authorization", async () => {
  const client = new MultiSearchClient(base, { apiKey: token });
  await assert.rejects(client.search({ query: "redirect" }), { code: "REDIRECT_BLOCKED", status: 307 });
  assert.deepEqual(foreignReceived, []);
});

test("bounds responses, timeouts, and surfaced error metadata", async () => {
  const client = new MultiSearchClient(base, { apiKey: token });
  await assert.rejects(client.search({ query: "throttle" }), (error) => {
    assert.ok(error instanceof MultiSearchError);
    assert.deepEqual(
      [error.code, error.status, error.requestId, error.traceId, error.retryable, error.retryAfterMs],
      ["RATE_LIMITED", 429, "from-body", traceId, true, 2000],
    );
    assert.ok(!String(error).includes(token));
    return true;
  });
  const bounded = new MultiSearchClient(base, { apiKey: token, maxResponseBytes: 64 });
  await assert.rejects(bounded.search({ query: "oversize" }), { code: "RESPONSE_TOO_LARGE" });
  const short = new MultiSearchClient(base, { apiKey: token, timeoutMs: 25 });
  await assert.rejects(short.search({ query: "timeout" }), { code: "TIMEOUT" });
  const control = new AbortController();
  control.abort();
  await assert.rejects(client.search({ query: "timeout" }, { signal: control.signal }), { code: "ABORTED" });
});

test("invalid input never reaches the network", async () => {
  const client = new MultiSearchClient(base, { apiKey: token });
  const count = received.length;
  for (const url of [
    "http://example.org", "http://localhost:8765", "https://user@example.org",
    "https://example.org?key=value",
  ]) assert.throws(() => new MultiSearchClient(url, { apiKey: token }), TypeError);
  assert.throws(() => new MultiSearchClient(base, { apiKey: "key\r\nInjected: x" }), TypeError);
  assert.throws(() => client.getJob("../unsafe"), TypeError);
  await assert.rejects(client.search({ query: "moon", request_id: "one" }, { requestId: "two" }), TypeError);
  await assert.rejects(client.search({ query: "月".repeat(12_000) }), { code: "REQUEST_TOO_LARGE" });
  await assert.rejects(new MultiSearchClient(base).search({ query: "moon" }), { code: "API_KEY_REQUIRED" });
  assert.equal(received.length, count);
});
