#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "../../../../..");
const state = path.join(root, ".ui-e2e-state/gate-you-real/runtime");
const out = "/home/tnunamak/.tmp/gw-product/gate/v3/gw-gate-you";
const baseUrl = process.env.GATE_YOU_BASE_URL || "http://127.0.0.1:18431";
const captureUrl = new URL(baseUrl);
if (
  captureUrl.hostname !== "127.0.0.1" ||
  Number(captureUrl.port) < 18000 ||
  Number(captureUrl.port) > 18999
) {
  throw new Error(
    "capture requires a throwaway loopback gateway on ports 18000–18999",
  );
}
const savedPublicUrl = "https://gate-you-copy.example.test";
const savedApiBaseUrl = `${savedPublicUrl}/v1`;
const cookiePath = path.join(state, "session-cookies.json");

const pages = [
  ["home", "/ui/home/"],
  ["models", "/ui/models/"],
  ["library", "/ui/library/"],
  ["usage", "/ui/usage/"],
];
const viewports = [
  ["1440", { width: 1440, height: 900 }],
  ["390", { width: 390, height: 844 }],
];

function clean(value) {
  return String(value)
    .replace(/(?:oak_|opk_)[A-Za-z0-9_-]+/g, "[REDACTED KEY]")
    .replace(/([?#&]token=)[^\\s"&]+/g, "$1[REDACTED]")
    .replace(/Bearer\\s+[A-Za-z0-9._-]+/gi, "Bearer [REDACTED]");
}

async function settle(page) {
  await page.waitForLoadState("domcontentloaded");
  await page
    .waitForLoadState("networkidle", { timeout: 15000 })
    .catch(() => {});
  await page
    .locator(
      '[aria-busy="true"], [aria-label^="Loading"], [data-slot="skeleton"]',
    )
    .first()
    .waitFor({ state: "hidden", timeout: 20000 })
    .catch(() => {});
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(700);
}

async function pageFacts(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const body = document.body;
    return {
      scrollWidth: Math.max(doc.scrollWidth, body.scrollWidth),
      clientWidth: doc.clientWidth,
      loading: document.querySelectorAll(
        '[aria-busy="true"], [data-slot="skeleton"]',
      ).length,
    };
  });
}

async function capturePage(
  context,
  role,
  pageName,
  pagePath,
  viewportName,
  viewport,
) {
  const page = await context.newPage();
  await page.setViewportSize(viewport);
  const events = { console: [], network: [] };
  page.on("console", (msg) => {
    if (msg.type() === "error") events.console.push(clean(msg.text()));
  });
  page.on("pageerror", (error) => events.console.push(clean(error.message)));
  page.on("requestfailed", (request) => {
    events.network.push({
      url: clean(request.url()),
      error: request.failure()?.errorText || "",
    });
  });
  page.on("response", (response) => {
    if (response.status() >= 400)
      events.network.push({
        url: clean(response.url()),
        status: response.status(),
      });
  });
  await page.goto(`${baseUrl}${pagePath}`);
  await settle(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  const facts = await pageFacts(page);
  const axe = await new AxeBuilder({ page }).analyze();
  const seriousAxe = axe.violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact || ""),
  );
  const publicBaseValue =
    pageName === "home"
      ? await page
          .locator("input")
          .evaluateAll(
            (inputs) =>
              inputs
                .map((input) => input.value)
                .find(
                  (value) => value === "https://gate-you-copy.example.test/v1",
                ) || "",
          )
          .catch(() => "")
      : "";
  const stem = `${role}-${pageName}-light-${viewportName}`;
  await page.screenshot({
    path: path.join(out, `${stem}.png`),
    fullPage: false,
  });
  await page.screenshot({
    path: path.join(out, `${stem}-full.png`),
    fullPage: true,
  });
  await page.close();
  return {
    role,
    page: pageName,
    viewport: viewportName,
    url: `${baseUrl}${pagePath}`,
    files: [`${stem}.png`, `${stem}-full.png`],
    console_errors: events.console.length,
    failed_requests: events.network.length,
    serious_axe: seriousAxe.map((violation) => violation.id),
    horizontal_overflow_px: Math.max(0, facts.scrollWidth - facts.clientWidth),
    loading_markers: facts.loading,
    public_base_input_matches_saved: publicBaseValue === savedApiBaseUrl,
  };
}

async function getJson(context, urlPath) {
  const response = await context.request.get(`${baseUrl}${urlPath}`);
  if (!response.ok()) return { ok: false, status: response.status() };
  return { ok: true, status: response.status(), json: await response.json() };
}

function countRows(payload) {
  if (Array.isArray(payload)) return payload.length;
  if (Array.isArray(payload?.items)) return payload.items.length;
  if (Array.isArray(payload?.rows)) return payload.rows.length;
  if (Array.isArray(payload?.models)) return payload.models.length;
  if (Array.isArray(payload?.data)) return payload.data.length;
  return 0;
}

async function apiFacts(context, role) {
  const session = await getJson(context, "/me/api/session");
  const home = await getJson(context, "/me/api/home");
  const models = await getJson(context, "/me/api/models/catalog");
  const library = await getJson(context, "/me/api/media?limit=40");
  const usage = await getJson(context, "/me/api/usage/summary");
  const mediaItems = library.ok
    ? library.json.items || library.json.assets || library.json.data || []
    : [];
  return {
    role,
    authenticated: Boolean(session.ok && session.json.authenticated),
    session_status: session.status,
    home_status: home.status,
    home_alias_count:
      home.ok && Array.isArray(home.json.aliases)
        ? home.json.aliases.length
        : 0,
    home_capability_count:
      home.ok && Array.isArray(home.json.capabilities)
        ? home.json.capabilities.length
        : 0,
    models_status: models.status,
    models_row_count:
      models.ok &&
      Array.isArray(models.json.aliases) &&
      Array.isArray(models.json.models)
        ? models.json.aliases.length + models.json.models.length
        : models.ok
          ? countRows(models.json)
          : 0,
    library_status: library.status,
    library_count: mediaItems.length,
    library_prompt_nonempty_count: mediaItems.filter(
      (item) => typeof item.prompt === "string" && item.prompt.trim(),
    ).length,
    usage_status: usage.status,
    usage_request_count:
      usage.ok &&
      usage.json.summary &&
      typeof usage.json.summary.requests === "number"
        ? usage.json.summary.requests
        : usage.ok && typeof usage.json.requests === "number"
          ? usage.json.requests
          : 0,
  };
}

async function main() {
  fs.mkdirSync(out, { recursive: true });
  if (!fs.existsSync(cookiePath)) {
    throw new Error(`missing session cookie file: ${cookiePath}`);
  }
  const cookies = JSON.parse(fs.readFileSync(cookiePath, "utf8"));
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/usr/bin/google-chrome",
  });
  const records = [];
  try {
    const api = [];
    for (const role of ["owner", "member"]) {
      for (const [viewportName, viewport] of viewports) {
        const context = await browser.newContext({
          viewport,
          colorScheme: "light",
          baseURL: baseUrl,
        });
        if (role === "owner") {
          const recoveryKey = fs.readFileSync(
            path.join(state, "owner-recovery-key"),
            "utf8",
          );
          const claim = await context.request.post(`${baseUrl}/setup/claim`, {
            form: { admin_key: recoveryKey },
          });
          if (!claim.ok())
            throw new Error(`copy owner recovery failed: ${claim.status()}`);
        } else {
          await context.addCookies([cookies.member]);
        }
        if (viewportName === "1440") api.push(await apiFacts(context, role));
        for (const [pageName, pagePath] of pages) {
          records.push(
            await capturePage(
              context,
              role,
              pageName,
              pagePath,
              viewportName,
              viewport,
            ),
          );
        }
        await context.close();
      }
    }

    const publicUrlObserved = records
      .filter((record) => record.page === "home")
      .every((record) => record.public_base_input_matches_saved);
    const ownerApi = api.find((record) => record.role === "owner") || {};
    const memberApi = api.find((record) => record.role === "member") || {};

    const summary = {
      captured_pages: records.length,
      screenshots: records.reduce(
        (count, record) => count + record.files.length,
        0,
      ),
      roles: ["owner", "member"],
      viewports: viewports.map(([name]) => name),
      pages: pages.map(([name]) => name),
      public_url_mismatch_verified: publicUrlObserved,
      saved_public_url: savedPublicUrl,
      authenticated_roles: api
        .filter((record) => record.authenticated)
        .map((record) => record.role),
      owner_home_alias_count: ownerApi.home_alias_count || 0,
      owner_home_capability_count: ownerApi.home_capability_count || 0,
      owner_models_row_count: ownerApi.models_row_count || 0,
      owner_library_count: ownerApi.library_count || 0,
      owner_library_prompt_nonempty_count:
        ownerApi.library_prompt_nonempty_count || 0,
      owner_usage_request_count: ownerApi.usage_request_count || 0,
      member_home_alias_count: memberApi.home_alias_count || 0,
      member_home_capability_count: memberApi.home_capability_count || 0,
      member_models_row_count: memberApi.models_row_count || 0,
      member_library_count: memberApi.library_count || 0,
      member_library_prompt_nonempty_count:
        memberApi.library_prompt_nonempty_count || 0,
      member_usage_request_count: memberApi.usage_request_count || 0,
      serious_axe_count: records.reduce(
        (count, record) => count + record.serious_axe.length,
        0,
      ),
      horizontal_overflow_count: records.filter(
        (record) =>
          record.viewport === "390" && record.horizontal_overflow_px > 0,
      ).length,
      loading_marker_count: records.reduce(
        (count, record) => count + record.loading_markers,
        0,
      ),
      console_error_count: records.reduce(
        (count, record) => count + record.console_errors,
        0,
      ),
      failed_request_count: records.reduce(
        (count, record) => count + record.failed_requests,
        0,
      ),
    };
    fs.writeFileSync(
      path.join(out, "manifest.json"),
      JSON.stringify({ summary, api, records }, null, 2),
    );
    console.log(JSON.stringify(summary, null, 2));
    if (!summary.public_url_mismatch_verified) process.exitCode = 2;
    if (
      !summary.authenticated_roles.includes("owner") ||
      !summary.authenticated_roles.includes("member")
    )
      process.exitCode = 2;
    if (!summary.owner_usage_request_count || !summary.owner_library_count)
      process.exitCode = 2;
    if (
      summary.serious_axe_count ||
      summary.horizontal_overflow_count ||
      summary.loading_marker_count
    )
      process.exitCode = 2;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(clean(error.stack || error.message));
  process.exit(1);
});
