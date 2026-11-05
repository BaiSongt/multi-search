import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
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
      request_id: "evidence-fixture", mode_executed: "evidence", partial: false, elapsed_ms: 100,
      result_count: 0, warnings: [], cache_hit: false, results: [],
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

test("角色工作区、密钥生命周期、图片归属与审计", { timeout: 120_000 }, async () => {
  const browser = await chromium.launch();
  const developer = await browser.newContext({ baseURL: base.origin, viewport: { width: 1280, height: 850 }, timezoneId: "Asia/Hong_Kong" });
  const admin = await browser.newContext({ baseURL: base.origin });
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
    await exportUsage(developerPage, developer);

    assert.deepEqual((await (await admin.request.get("/gateway/keys")).json()).keys, []);
    await adminPage.goto("/admin/providers");
    await adminPage.getByRole("region", { name: "Provider 状态和运行指标" }).waitFor();
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
    await adminPage.getByLabel("筛选本页租户").fill(tenant);
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
