import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = fileURLToPath(new URL("../../", import.meta.url));
const base = new URL(process.env.MULTISEARCH_TEST_CONSOLE_URL ?? "http://127.0.0.1:3000");
if (!["http:", "https:"].includes(base.protocol) || base.hostname !== "127.0.0.1" || base.username || base.password || base.pathname !== "/" || base.search || base.hash) {
  throw new Error("浏览器回归只允许使用回环 Console 地址");
}

function provision(role) {
  const tenant = `browser-${randomBytes(6).toString("hex")}`;
  const email = `${tenant}@example.invalid`;
  const password = randomBytes(24).toString("base64url");
  const result = spawnSync("docker", [
    "compose", "exec", "-T", "multisearch-api", "python", "-m", "multisearch.keys", "user-add",
    "--tenant", tenant, "--email", email, "--role", role, "--password-stdin",
  ], { cwd: root, input: password, timeout: 20_000, encoding: "utf8" });
  assert.equal(result.status, 0, "无法签发浏览器测试账号");
  return { tenant, email, password };
}

async function signIn(page, account) {
  await page.getByLabel("租户标识").fill(account.tenant);
  await page.getByLabel("邮箱").fill(account.email);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
}

async function session(context) {
  const response = await context.request.get("/gateway/me");
  assert.equal(response.status(), 200);
  return await response.json();
}

async function waitForTitle(page, name) {
  await page.getByRole("heading", { level: 1, name, exact: true }).waitFor();
}

async function verifySingleDisplay(page) {
  const dialog = page.getByRole("dialog", { name: "保存 API Key" });
  const secret = dialog.locator("[data-secret]");
  await secret.waitFor();
  const value = await secret.textContent();
  assert.ok(value && /^msk_[a-f0-9]{32}\.[A-Za-z0-9_-]{43}$/.test(value), "创建响应应一次性展示密钥");
  await dialog.getByRole("button", { name: "已安全保存" }).click();
  await secret.waitFor({ state: "detached" });
  const response = await page.request.get("/gateway/keys");
  assert.equal(response.status(), 200);
  assert.ok(!(await response.text()).includes(value), "列表不能返回 API Key 明文");
  return value;
}

async function keySearch(context, key) {
  return context.request.post("/gateway/search", {
    headers: { Authorization: `Bearer ${key}` },
    data: { query: "browser-cache-check", mode: "local" },
  });
}

async function unknownImage(page) {
  const fixture = {
    request_id: "image-ui-fixture",
    mode_executed: "standard",
    partial: false,
    elapsed_ms: 40,
    result_count: 1,
    warnings: [],
    cache_hit: false,
    results: [{
      result_id: "image-fixture",
      kind: "image",
      title: "示例图片",
      url: "https://example.org/source",
      source_type: "web",
      provider_ids: ["fixture"],
      retrieved_at: "2026-11-05T00:00:00Z",
      warnings: [],
      image: {
        original_url: "https://example.org/photo.jpg",
        thumbnail_url: "javascript:alert(1)",
        source_page_url: "https://example.org/source",
        source_site_name: "Example",
        source_site_domain: "example.org",
        search_engines: ["fixture"],
        width: 100,
        height: 100,
        creator: null,
        copyright_holder: null,
        license_name: null,
        license_url: null,
        attribution_text: null,
        rights_status: "unknown",
        rights_checked_at: null,
        rights_evidence_url: null,
        source_page_verified: false,
      },
    }],
  };
  await page.route("**/gateway/playground", async (route) => {
    assert.deepEqual(route.request().postDataJSON().modalities, ["image"]);
    await route.fulfill({ json: fixture });
  });
  await page.goto("/console/images");
  await page.getByLabel("关键词").fill("归属测试");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.locator('[data-rights-status="unknown"]').waitFor();
  await page.getByText("暂无可用预览").waitFor();
  assert.equal(await page.locator('img[src^="javascript:"]').count(), 0);
  await page.getByRole("button", { name: "图片详情" }).click();
  const dialog = page.getByRole("dialog", { name: "图片归属与许可" });
  await dialog.getByText("授权未知。使用前请从来源页确认适用许可。").waitFor();
  assert.equal(await dialog.getByRole("link", { name: "https://example.org/source" }).getAttribute("href"), "https://example.org/source");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  await page.unroute("**/gateway/playground");
}

async function evidenceResult(page) {
  const origin = "https://example.org/evidence";
  await page.route("**/gateway/capabilities", (route) => route.fulfill({ json: {
    modes: { auto: "available", evidence: "available" },
    source_types: { web: "available" },
    modalities: { text: "available" },
    network_scopes: { public: "available" },
    combinations: [{ source_type: "web", modality: "text" }],
    limits: { modes: { evidence: { max_latency_ms: 45000, max_results: 10 } }, max_image_metadata_fetches: 0 },
  } }));
  await page.route("**/gateway/playground", async (route) => {
    assert.equal(route.request().postDataJSON().mode, "evidence");
    await route.fulfill({ json: {
      request_id: "evidence-fixture", mode_executed: "evidence", partial: true, elapsed_ms: 100,
      result_count: 1, warnings: ["OCR_PENDING"], cache_hit: false, results: [{
        kind: "text", result_id: "scanned-fixture", title: "扫描版示例", url: "https://example.org/scanned.pdf",
        source_type: "web", provider_ids: ["fixture"], retrieved_at: "2026-11-05T00:00:00Z",
        snippet: null, warnings: ["OCR_PENDING"], evidence_status: "ocr_pending",
      }],
      evidence_records: [{
        evidence_id: "evidence-fixture-1", result_id: null, origin_url: origin, canonical_url: origin,
        document_identifier: null, retrieved_at: "2026-11-05T00:00:00Z", published_at: null,
        title: "示例来源", content_type: "text/html", evidence_text: "用于核对的证据摘录。",
        locator: { kind: "html_text", section: "检索范围", text_offset_start: 10, text_offset_end: 20, page_number: null },
        content_sha256: "0".repeat(64), extraction_method: "static_html", extraction_version: "1",
        verification_status: "content_extracted", access_policy: "metadata_and_excerpt",
      }],
    } });
  });
  await page.goto("/console/playground");
  await page.getByLabel("关键词").fill("证据定位测试");
  await page.getByLabel("搜索深度").selectOption("evidence");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.getByRole("article").filter({ hasText: "扫描版示例" }).getByText("等待 OCR", { exact: true }).waitFor();
  await page.getByRole("button", { name: "证据详情" }).click();
  const dialog = page.getByRole("dialog", { name: "可定位证据" });
  await dialog.getByText("用于核对的证据摘录。").waitFor();
  await dialog.getByText("10–20", { exact: true }).waitFor();
  assert.equal(await dialog.getByRole("link", { name: origin }).getAttribute("href"), origin);
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  await page.unroute("**/gateway/playground");
  await page.unroute("**/gateway/capabilities");
}

async function researchJob(page, csrf) {
  const publicId = "dc11817c-17f1-4488-848e-bda75b0aa471";
  const localId = "93fd7a11-4de8-416e-a33f-89ce2618f299";
  const traceId = "b".repeat(32);
  const origin = "https://example.org/research";
  const budget = { max_latency_ms: 50000, max_search_rounds: 2, max_pages: 3, max_results: 10,
    max_image_results: 0, max_image_metadata_fetches: 0, max_llm_tokens: 500 };
  const localBudget = { ...budget, max_pages: 0, max_llm_tokens: 0 };
  const record = {
    evidence_id: "research-evidence-1", result_id: null, origin_url: origin, canonical_url: origin,
    document_identifier: null, retrieved_at: "2026-11-05T00:00:00Z", published_at: null,
    title: "研究来源", content_type: "text/html", evidence_text: "来源原文包含测试证据。",
    locator: { kind: "html_text", section: null, text_offset_start: 12, text_offset_end: 24, page_number: null },
    content_sha256: "1".repeat(64), extraction_method: "static_html", extraction_version: "1",
    verification_status: "content_extracted", access_policy: "metadata_and_excerpt",
  };
  const result = {
    mode: "deep", summary: "完成两轮检索并保留一条可定位证据。", planned_rounds: 2, results: [],
    rounds: [
      { number: 1, step: "original", mode: "evidence", status: "completed", elapsed_ms: 100, result_count: 1,
        evidence_refs: [record.evidence_id], error_code: null },
      { number: 2, step: "limitations", mode: "evidence", status: "partial", elapsed_ms: 200, result_count: 0,
        evidence_refs: [], error_code: null },
    ],
    evidence_records: [record], warnings: ["第二轮仅返回部分结果。"], comparisons: [],
    findings: [{ title: record.title, origin_url: origin, excerpt: record.evidence_text,
      evidence_refs: [record.evidence_id], verification_status: "content_extracted" }],
    model_findings: [{ claim: "模型综合结论", evidence_refs: [record.evidence_id], verification_status: "model_synthesis_unverified" }],
    budget, usage: { search_rounds: 2, pages_attempted: null, pages_charged: 3, pages_fetched: 1,
      image_metadata_fetches: 0, image_metadata_charged: 0, llm_tokens: null, llm_tokens_reserved: 500 },
  };
  function job(id, status, stage, completed, report = null) {
    return { job_id: id, tenant_id: publicId, kind: "research", status, created_at: "2026-11-05T00:00:00Z",
      updated_at: "2026-11-05T00:00:01Z", deadline_at: "2026-11-05T00:00:50Z", attempts: 1, max_attempts: 3,
      request_id: "research-request-fixture", trace_id: traceId,
      progress: { stage, completed, total: 2, pages_fetched: report ? 1 : 0,
        research_budget: { search_rounds: completed, pages_charged: 0, image_metadata_charged: 0, llm_tokens_reserved: 0 } },
      result: report, error: null };
  }

  // 独立检查真实网关的路径白名单及上游跟踪标识。
  const missing = await page.request.get(`/gateway/console/jobs/${publicId}`);
  assert.equal(missing.status(), 404);
  assert.equal((await missing.json()).code, "JOB_NOT_FOUND");
  assert.match(missing.headers()["x-trace-id"], /^[a-f0-9]{32}$/);
  for (const [method, path] of [["GET", `${publicId}/cancel`], ["POST", publicId], ["GET", publicId.replaceAll("-", "")]]) {
    const rejected = await page.request.fetch(`/gateway/console/jobs/${path}`, { method });
    assert.equal(rejected.status(), 404);
    assert.equal(rejected.headers()["x-trace-id"], undefined);
  }

  let available = false;
  let publicPolls = 0;
  let finishPublic = false;
  let cancelled = false;
  const capabilityRoute = "**/gateway/capabilities";
  const submitRoute = "**/gateway/playground";
  const jobsRoute = "**/gateway/console/jobs/**";
  await page.route(capabilityRoute, (route) => route.fulfill({ json: {
    research: available ? "available" : "unavailable", modes: { auto: "available", deep: "available" },
    source_types: { web: "available", local: "available" }, modalities: { text: "available" },
    network_scopes: { public: "available", offline: "available" },
    combinations: [{ source_type: "web", modality: "text" }, { source_type: "local", modality: "text" }],
    limits: { modes: { deep: { max_latency_ms: 900000, max_results: 30 } }, max_image_metadata_fetches: 0 },
  } }));
  await page.route(submitRoute, async (route) => {
    const req = route.request();
    assert.equal(req.method(), "POST");
    assert.equal(req.headers()["x-csrf-token"], csrf);
    assert.equal(req.headers().authorization, undefined);
    const body = req.postDataJSON();
    assert.equal(body.mode, "deep");
    assert.equal(body.max_search_rounds, 2);
    const local = body.source_types[0] === "local";
    assert.equal(body.network_scope, local ? "offline" : "public");
    assert.equal(body.max_pages, local ? undefined : 3);
    assert.equal(body.max_llm_tokens, local ? undefined : 500);
    const id = local ? localId : publicId;
    await route.fulfill({ status: 202, json: {
      job_id: id, status: "queued", status_url: `/api/v1/console/jobs/${id}`, budget: local ? localBudget : budget, trace_id: traceId,
    } });
  });
  await page.route(jobsRoute, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    assert.equal(req.headers()["x-csrf-token"], csrf);
    if (path.endsWith(`/${localId}/cancel`)) {
      assert.equal(req.method(), "POST");
      cancelled = true;
      await route.fulfill({ json: job(localId, "cancelling", "searching", 0) });
    } else {
      assert.equal(req.method(), "GET");
      if (path.endsWith(`/${localId}`)) {
        await route.fulfill({ json: cancelled ? job(localId, "cancelled", "cancelled", 0) : job(localId, "queued", "queued", 0) });
      } else {
        assert.ok(path.endsWith(`/${publicId}`));
        publicPolls += 1;
        await route.fulfill({ json: finishPublic ? job(publicId, "partial", "completed", 2, result) : job(publicId, "running", "searching", 1) });
      }
    }
  });

  const deep = page.getByLabel("搜索深度");
  const task = page.getByRole("region", { name: "深度研究任务" });
  const report = page.getByRole("region", { name: "研究报告" });
  const submit = page.getByRole("button", { name: "启动深度研究" });
  try {
    await page.goto("/console/playground");
    await deep.locator('option[value="quick"]').waitFor({ state: "detached" });
    assert.equal(await deep.locator('option[value="deep"]').count(), 0);
    available = true;
    await page.reload();
    await deep.selectOption("deep");
    await page.getByLabel("关键词").fill("浏览器深度研究测试");
    await page.getByText("高级参数与 API Key", { exact: true }).click();
    assert.ok(await page.getByLabel("API Key", { exact: true }).isDisabled());
    await page.getByLabel("最多检索轮次").fill("2");
    await page.getByLabel("最多抓取页数").fill("3");
    await page.getByLabel("模型 Token 上限").fill("500");
    await submit.click();
    await task.getByText("检索与证据提取 · 1/2 轮", { exact: true }).waitFor();
    await task.getByText("最多 3", { exact: true }).waitFor();
    await task.getByText(traceId, { exact: true }).waitFor();
    await task.getByText("research-request-fixture", { exact: true }).waitFor();
    finishPublic = true;
    await task.getByText("部分完成", { exact: true }).waitFor();
    await task.getByText("未报告 / 500", { exact: true }).waitFor();
    assert.ok(publicPolls >= 2);
    await report.getByText(result.summary).waitFor();
    await report.getByRole("heading", { name: "模型综合 · 未核验" }).waitFor();
    assert.equal(await report.getByRole("link", { name: record.title }).getAttribute("href"), origin);
    await report.getByRole("button", { name: "证据 · 研究来源" }).first().click();
    const dialog = page.getByRole("dialog", { name: "可定位证据" });
    await dialog.getByText(record.evidence_text).waitFor();
    await dialog.getByText("12–24", { exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    await page.getByLabel(/^来源/).selectOption("local");
    await page.getByText(/本地研究只读取离线索引/).waitFor();
    await submit.click();
    await task.getByRole("button", { name: "取消研究", exact: true }).click();
    await task.getByText("已取消", { exact: true }).waitFor();
    assert.ok(cancelled);
    await report.waitFor({ state: "detached" });
  } finally {
    await page.unroute(jobsRoute);
    await page.unroute(submitRoute);
    await page.unroute(capabilityRoute);
  }
}

async function exportUsage(page, context) {
  const actor = await session(context);
  const id = `-browser-csv-${randomBytes(4).toString("hex")}`;
  const data = { query: "browser-csv", mode: "local", request_id: id };
  const search = await context.request.post("/gateway/playground", {
    headers: { Origin: base.origin, "X-CSRF-Token": actor.csrf_token },
    data,
  });
  assert.equal(search.status(), 200);
  await page.goto("/console/usage");
  await page.getByRole("heading", { name: "有效额度上限" }).waitFor();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出筛选调用 CSV" }).click();
  const download = await downloaded;
  assert.equal(download.suggestedFilename(), "multisearch-requests.csv");
  const stream = await download.createReadStream();
  const buffers = [];
  for await (const chunk of stream) buffers.push(chunk);
  const csv = Buffer.concat(buffers).toString("utf8");
  assert.ok(csv.startsWith("\uFEFF"));
  assert.ok(csv.includes(`"'${id}"`), "导出不得把以减号开头的请求 ID 当作公式");
}

async function verifyInvitation(browser, adminPage, tenant, tenantId) {
  await adminPage.goto(`/admin/tenants/${tenantId}`);
  await waitForTitle(adminPage, "租户成员与邀请");
  await adminPage.getByRole("button", { name: "邀请成员" }).click();
  const dialog = adminPage.getByRole("dialog", { name: "邀请成员" });
  const account = { tenant, email: `${tenant}-invitee@example.invalid`, password: randomBytes(24).toString("base64url") };
  await dialog.getByLabel("邀请邮箱").fill(account.email);
  await dialog.getByRole("button", { name: "生成邀请链接" }).click();
  const secret = adminPage.locator("[data-invitation-link]");
  await secret.waitFor();
  const url = new URL(await secret.textContent());
  assert.equal(url.origin, base.origin);
  assert.equal(url.pathname, "/auth/invite");
  assert.equal(url.search, "");
  assert.ok(url.hash.startsWith("#token="), "邀请令牌应留在浏览器片段中");
  await adminPage.getByRole("button", { name: "已保存邀请链接" }).click();
  await secret.waitFor({ state: "detached" });
  const invitee = await browser.newContext({ baseURL: base.origin });
  try {
    const page = await invitee.newPage();
    await page.goto(url.href);
    await page.getByLabel("账户密码", { exact: true }).fill(account.password);
    assert.equal(new URL(page.url()).hash, "");
    await page.getByLabel("确认密码", { exact: true }).fill(account.password);
    await page.getByRole("button", { name: "接受邀请" }).click();
    await page.getByText(`已加入租户 ${tenant}。`).waitFor();
    await page.getByRole("link", { name: "进入登录" }).click();
    await signIn(page, account);
    await waitForTitle(page, "我的概览");
    const actor = await session(invitee);
    assert.equal(actor.tenant_id, tenantId);
    assert.equal(actor.role, "developer");
  } finally {
    await invitee.close();
  }
  await adminPage.reload();
  const member = adminPage.getByRole("region", { name: "团队成员" }).getByRole("row").filter({ hasText: account.email });
  await member.getByRole("button", { name: "调整权限" }).click();
  await adminPage.getByRole("dialog").getByLabel("角色").selectOption("tenant_admin");
  await adminPage.getByRole("button", { name: "预览调整" }).click();
  await adminPage.getByRole("button", { name: "确认调整" }).click();
  await member.getByText("租户管理员", { exact: true }).waitFor();
}

async function verifyCancellation(page, tenantId) {
  const jobId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
  let status = "queued";
  const job = () => ({
    job_id: jobId, tenant_id: tenantId, kind: "fetch", status,
    created_at: "2026-11-05T00:00:00Z", updated_at: "2026-11-05T00:00:01Z", deadline_at: "2026-11-05T00:01:00Z",
    attempts: 0, max_attempts: 3, progress: { stage: status, completed: 0, total: 2 }, result: null, error: null,
  });
  await page.route("**/gateway/admin/jobs?*", (route) => route.fulfill({ json: { items: [job()], next_cursor: null } }));
  await page.route(`**/gateway/admin/jobs/${jobId}`, (route) => route.fulfill({ json: job() }));
  await page.route(`**/gateway/admin/jobs/${jobId}/cancel`, async (route) => {
    assert.equal(route.request().method(), "POST");
    assert.ok(route.request().headers()["x-csrf-token"]);
    status = "cancelled";
    await route.fulfill({ json: job() });
  });
  await page.goto("/admin/jobs");
  await page.getByRole("region", { name: "任务列表" }).getByRole("button", { name: "详情", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "任务详情" });
  await dialog.getByRole("button", { name: "取消任务", exact: true }).click();
  await dialog.getByRole("button", { name: "确认取消任务" }).click();
  await dialog.getByText("已取消", { exact: true }).waitFor();
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  await page.unroute("**/gateway/admin/jobs?*");
  await page.unroute(`**/gateway/admin/jobs/${jobId}`);
  await page.unroute(`**/gateway/admin/jobs/${jobId}/cancel`);
}

async function verifyOperations(page, tenantId) {
  await page.goto("/admin/jobs");
  await waitForTitle(page, "任务与队列");
  assert.equal((await page.request.get("/gateway/admin/jobs")).status(), 200);

  await page.goto("/admin/images");
  await waitForTitle(page, "图片来源质量");
  await page.getByText(/许可核验比例/).waitFor();

  await page.goto("/admin/settings");
  await page.getByRole("heading", { name: "会话安全" }).waitFor();
  await page.getByText("CSRF 校验", { exact: true }).waitFor();

  await page.goto("/admin/routing");
  await page.getByText(/当前版本 [0-9]+/).waitFor();
  await page.getByRole("button", { name: "试算路由" }).click();
  await page.getByText(/执行模式.+时限.+最多/).waitFor();

  await page.goto("/admin/cache");
  await page.getByLabel("缓存租户 ID").fill(tenantId);
  await page.getByRole("button", { name: "切换租户" }).click();
  await page.getByText(tenantId, { exact: true }).first().waitFor();
  await page.getByRole("button", { name: "预览失效" }).click();
  const confirm = page.getByRole("dialog", { name: "确认使缓存失效" });
  await confirm.getByText(tenantId, { exact: true }).waitFor();
  await confirm.getByRole("button", { name: "确认失效" }).click();
  await page.getByText(/图片缓存已更新到代数/).waitFor();
}

const traceSeed = `
INSERT INTO usage_events (
  id, tenant_id, principal_kind, principal_id, user_id, request_id, trace_id,
  mode, modalities, source_types, status, code, elapsed_ms, replay_payload, reservation_expires_at
) VALUES (
  :'usage_id'::UUID, :'tenant_id'::UUID, 'session', :'user_id'::UUID, :'user_id'::UUID,
  :'request_id', :'trace_id', 'standard', ARRAY['text'], ARRAY['academic'], 'failed',
  'PROVIDER_UNAVAILABLE', 95, '{"query":"trace-redaction-fixture"}', now()
);`;
const traceRuns = `
INSERT INTO provider_runs (usage_id, ordinal, provider_id, status, elapsed_ms, count, reason_codes)
VALUES (:'usage_id'::UUID, 0, 'searxng', 'failed', 80, 0,
  ARRAY['upstream_captcha', 'upstream_rate_limited']);`;

function traceFixture(actor, id, traceId, sql) {
  const result = spawnSync("docker", [
    "compose", "exec", "-T", "postgres", "psql", "-Xq", "-U", "multisearch", "-d", "multisearch",
    "-v", "ON_ERROR_STOP=1", "-v", `usage_id=${id}`, "-v", `trace_id=${traceId}`,
    "-v", `tenant_id=${actor.tenant_id}`, "-v", `user_id=${actor.user_id}`, "-v", `request_id=trace-browser-${id}`,
  ], { cwd: root, input: sql, timeout: 15_000, encoding: "utf8" });
  assert.equal(result.status, 0, "无法准备或清理请求追踪测试记录");
}

async function verifyRequestTrace(page, developer, developerSession, operations, operationsSession) {
  const id = randomUUID();
  const traceId = randomBytes(16).toString("hex");
  const ownId = randomUUID();
  const ownTrace = randomBytes(16).toString("hex");
  const route = `/gateway/admin/requests/${id}`;
  try {
    traceFixture(developerSession, id, traceId, `BEGIN;${traceSeed}${traceRuns}COMMIT;`);
    traceFixture(operationsSession, ownId, ownTrace, traceSeed);
    assert.equal((await developer.request.get(route)).status(), 403);
    const denied = await operations.request.get(route);
    assert.equal(denied.status(), 404);
    assert.equal((await denied.json()).code, "REQUEST_NOT_FOUND");
    const own = await operations.request.get(`/gateway/admin/requests/${ownId}`);
    assert.equal(own.status(), 200);
    assert.deepEqual((await own.json()).provider_runs, []);

    await page.goto("/admin/requests");
    await page.getByLabel("追踪 ID", { exact: true }).fill(traceId.toUpperCase());
    await page.getByRole("combobox", { name: /^来源/ }).selectOption("academic");
    const filtered = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/gateway/admin/requests" && url.searchParams.get("trace_id") === traceId
        && url.searchParams.get("source_type") === "academic";
    });
    await page.getByRole("button", { name: "应用筛选" }).click();
    const listing = await filtered;
    assert.equal(listing.status(), 200);
    assert.deepEqual((await listing.json()).items.map((row) => row.id), [id]);
    await page.getByRole("row").filter({ hasText: `trace-browser-${id}` }).getByRole("button", { name: "详情" }).click();
    const dialog = page.getByRole("dialog", { name: "请求详情" });
    await dialog.getByText(traceId, { exact: true }).waitFor();
    const provider = dialog.getByRole("row").filter({ hasText: "searxng" });
    await provider.getByText("upstream_captcha", { exact: true }).waitFor();
    await provider.getByText("upstream_rate_limited", { exact: true }).waitFor();
    const response = await page.request.get(route);
    assert.equal(response.status(), 200);
    assert.equal(response.headers()["cache-control"], "no-store");
    const body = await response.text();
    assert.ok(!body.includes("trace-redaction-fixture"));
    assert.deepEqual(JSON.parse(body).provider_runs, [{
      provider_id: "searxng", status: "failed", elapsed_ms: 80, count: 0,
      reason_codes: ["upstream_captcha", "upstream_rate_limited"],
    }]);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
  } finally {
    const cleanup = "DELETE FROM usage_events WHERE id = :'usage_id'::UUID AND tenant_id = :'tenant_id'::UUID;";
    traceFixture(developerSession, id, traceId, cleanup);
    traceFixture(operationsSession, ownId, ownTrace, cleanup);
  }
}

test("角色工作区、密钥生命周期、图片归属与审计", { timeout: 120_000 }, async () => {
  const browser = await chromium.launch();
  const developer = await browser.newContext({ baseURL: base.origin, viewport: { width: 1280, height: 850 }, timezoneId: "Asia/Hong_Kong" });
  const admin = await browser.newContext({ baseURL: base.origin });
  const operations = await browser.newContext({ baseURL: base.origin });
  const developerPage = await developer.newPage();
  const adminPage = await admin.newPage();
  const errors = [];
  for (const page of [developerPage, adminPage]) {
    page.on("pageerror", (error) => errors.push(`${new URL(page.url()).pathname} ${error.message.split("\n")[0]}`));
  }

  const tenantIds = [];
  const fixtureAccounts = [];
  let adminSession;
  try {
    const developerAccount = provision("developer");
    fixtureAccounts.push(developerAccount);
    const adminAccount = provision("platform_admin");
    fixtureAccounts.push(adminAccount);
    await adminPage.goto("/auth");
    await signIn(adminPage, adminAccount);
    await waitForTitle(adminPage, "服务总览");
    adminSession = await session(admin);

    await developerPage.goto("/admin/providers");
    await developerPage.waitForURL("**/auth?next=*");
    await signIn(developerPage, developerAccount);
    await developerPage.getByText("当前角色无权访问此页面。").waitFor();
    assert.equal((await developer.request.get("/gateway/admin/providers")).status(), 403);
    assert.equal((await developer.request.get("/gateway/team/members")).status(), 403);
    const developerSession = await session(developer);
    tenantIds.push(developerSession.tenant_id);
    const operationsAccount = provision("operations_admin");
    fixtureAccounts.push(operationsAccount);
    const operationsLogin = await operations.request.post("/gateway/auth/login", {
      data: operationsAccount, headers: { Origin: base.origin },
    });
    assert.equal(operationsLogin.status(), 200);
    const operationsSession = await session(operations);
    tenantIds.push(operationsSession.tenant_id);
    const cookie = (await developer.cookies()).find((item) => item.name === "multisearch_session");
    assert.ok(cookie?.httpOnly && cookie.sameSite === "Lax");
    assert.equal((await developer.request.post("/gateway/auth/refresh", {
      headers: { Origin: "http://cross-origin.invalid", "X-CSRF-Token": developerSession.csrf_token },
    })).status(), 403);

    await developerPage.goto("/console/keys");
    await waitForTitle(developerPage, "API 密钥");
    await developerPage.getByRole("button", { name: "新建密钥" }).click();
    const create = developerPage.getByRole("dialog", { name: "新建密钥" });
    await create.getByLabel("密钥名称").fill("浏览器回归密钥");
    await create.getByRole("button", { name: "生成密钥" }).click();
    const original = await verifySingleDisplay(developerPage);
    assert.equal((await keySearch(developer, original)).status(), 200);
    await developerPage.getByRole("button", { name: "轮换", exact: true }).click();
    await developerPage.getByRole("button", { name: "确认轮换" }).click();
    const rotated = await verifySingleDisplay(developerPage);
    assert.ok(rotated !== original);
    assert.equal((await keySearch(developer, original)).status(), 401);
    assert.equal((await keySearch(developer, rotated)).status(), 200);

    // 使用真实列表游标，在浏览器回归中缩小页长以减少临时凭据。
    await developerPage.route("**/gateway/keys?*", async (route) => {
      const url = new URL(route.request().url());
      url.searchParams.set("limit", "1");
      const response = await route.fetch({ url: url.href });
      await route.fulfill({ response });
    });
    await developerPage.reload();
    await developerPage.getByRole("button", { name: "下一页" }).click();
    await developerPage.getByText("已吊销", { exact: true }).last().waitFor();
    await developerPage.getByRole("button", { name: "上一页" }).click();
    await developerPage.getByRole("button", { name: "吊销", exact: true }).click();
    await developerPage.getByRole("button", { name: "确认吊销" }).click();
    await developerPage.getByRole("dialog").waitFor({ state: "detached" });
    assert.equal((await keySearch(developer, rotated)).status(), 401);
    await developerPage.unroute("**/gateway/keys?*");

    await developerPage.goto("/console/playground");
    await developerPage.getByLabel("关键词").fill("browser-session-search");
    await developerPage.getByLabel("搜索深度").selectOption("local");
    await developerPage.getByRole("button", { name: "搜索", exact: true }).click();
    await developerPage.getByRole("region", { name: "搜索摘要" }).waitFor();
    await developerPage.goto("/console/usage");
    await waitForTitle(developerPage, "调用量与额度");
    await developerPage.getByRole("heading", { name: "有效额度上限" }).waitFor();
    await developerPage.getByRole("button", { name: "切换浅色或深色主题" }).click();
    const theme = await developerPage.locator("html").getAttribute("data-theme");
    assert.ok(theme === "dark" || theme === "light");
    await developerPage.reload();
    assert.equal(await developerPage.locator("html").getAttribute("data-theme"), theme);
    await developerPage.setViewportSize({ width: 390, height: 844 });
    assert.ok(await developerPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await unknownImage(developerPage);
    await evidenceResult(developerPage);
    await researchJob(developerPage, developerSession.csrf_token);
    await exportUsage(developerPage, developer);

    assert.deepEqual((await (await admin.request.get("/gateway/keys")).json()).keys, []);
    await adminPage.goto("/admin/providers");
    await adminPage.getByRole("region", { name: "Provider 状态和运行指标" }).waitFor();
    await verifyRequestTrace(adminPage, developer, developerSession, operations, operationsSession);
    await adminPage.goto("/admin/tenants");
    await adminPage.getByRole("button", { name: "新建租户" }).click();
    const tenant = `browser-${randomBytes(6).toString("hex")}`;
    await adminPage.getByRole("dialog").getByLabel("租户标识").fill(tenant);
    const created = adminPage.waitForResponse((response) => response.url().endsWith("/gateway/admin/tenants") && response.request().method() === "POST");
    await adminPage.getByRole("button", { name: "创建租户" }).click();
    const createdResponse = await created;
    assert.equal(createdResponse.status(), 201);
    const tenantId = (await createdResponse.json()).id;
    tenantIds.push(tenantId);
    await adminPage.getByText(tenant, { exact: true }).waitFor();
    await adminPage.goto("/admin/quotas");
    await adminPage.getByLabel("筛选租户").fill(tenant);
    await adminPage.getByRole("button", { name: "编辑额度" }).click();
    await adminPage.getByRole("dialog").getByLabel("每日请求", { exact: true }).fill("99");
    await adminPage.getByRole("button", { name: "预览变更" }).click();
    await adminPage.getByText("确认变更", { exact: true }).waitFor();
    await adminPage.getByRole("button", { name: "确认提交" }).click();
    await adminPage.getByRole("dialog").waitFor({ state: "detached" });
    await adminPage.goto("/admin/audit");
    await adminPage.getByText("quota.update", { exact: true }).first().waitFor();
    await verifyInvitation(browser, adminPage, tenant, tenantId);
    await verifyOperations(adminPage, tenantId);
    await verifyCancellation(adminPage, tenantId);

    await developerPage.goto("/console/settings");
    const refreshed = developerPage.waitForResponse("**/gateway/auth/refresh");
    const reloaded = developerPage.waitForEvent("load");
    await developerPage.getByRole("button", { name: "续期会话" }).click();
    assert.equal((await refreshed).status(), 200);
    await reloaded;
    await developerPage.getByRole("button", { name: "登出", exact: true }).click();
    await developerPage.waitForURL("**/auth");
    assert.equal((await developer.request.get("/gateway/me")).status(), 401);
    assert.deepEqual(errors, []);
  } finally {
    if (adminSession) {
      const headers = { Origin: base.origin, "X-CSRF-Token": adminSession.csrf_token };
      for (const tenantId of tenantIds) {
        await admin.request.patch(`/gateway/admin/tenants/${tenantId}`, {
          headers,
          data: { enabled: false },
          timeout: 5_000,
        }).catch(() => {});
      }
      await admin.request.post("/gateway/auth/logout", { headers, timeout: 5_000 }).catch(() => {});
    }
    await browser.close();
    for (const account of fixtureAccounts) {
      const disabled = spawnSync("docker", [
        "compose", "exec", "-T", "multisearch-api", "python", "-m", "multisearch.keys", "user-disable",
        "--tenant", account.tenant, "--email", account.email,
      ], { cwd: root, timeout: 20_000, encoding: "utf8" });
      if (disabled.status !== 0) console.warn("本次浏览器验收账户未自动停用");
    }
  }
});
