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
    tenantIds.push((await createdResponse.json()).id);
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
