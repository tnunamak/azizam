#!/usr/bin/env node
// Prompt memory screens on the household fixture, for the contact sheet.
// Usage: node e2e/prompt-memory-capture.mjs <manifest.json> <out-dir> [only]
// Start the fixture first: UI_SEED_PROFILE=household scripts/ui/serve.py.
// Each state is captured at 390 and 1440, light and dark.
import { chromium } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";

const [manifestPath, outDir, only] = process.argv.slice(2);
if (!manifestPath || !outDir) {
  console.error("usage: prompt-memory-capture.mjs <manifest> <out> [only]");
  process.exit(2);
}
const seed = JSON.parse(await readFile(manifestPath, "utf8"));
const base = seed.base_url;
await mkdir(outDir, { recursive: true });
const kept = seed.assets.find(
  (a) => a.owner === "robin" && a.kind === "image" && a.prompt_kept,
);
const notKept = seed.assets.find((a) => a.owner === "robin" && !a.prompt_kept);
const samKept = seed.assets.find(
  (a) => a.owner === "sam" && a.kind === "image" && a.prompt_kept,
);

const browser = await chromium.launch();
const sizes = [
  { name: "390", width: 390, height: 844 },
  { name: "1440", width: 1440, height: 900 },
];

// Sign in once per person; the gateway throttles repeated sign-ins.
const signedIn = new Map();
async function storageFor(email) {
  if (signedIn.has(email)) return signedIn.get(email);
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(`${base}/ui/sign-in/`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(seed.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL((url) => !url.pathname.includes("/sign-in"), {
    timeout: 15_000,
  });
  const state = await ctx.storageState();
  await ctx.close();
  signedIn.set(email, state);
  return state;
}

async function context(email, theme, size) {
  const ctx = await browser.newContext({
    viewport: { width: size.width, height: size.height },
    colorScheme: theme,
    reducedMotion: "reduce",
    deviceScaleFactor: 1,
    storageState: await storageFor(email),
  });
  await ctx.addInitScript(
    (value) => localStorage.setItem("theme", value),
    theme,
  );
  // The household's clock, so ages read as the fixture means them.
  await ctx.clock.setFixedTime(new Date(seed.now * 1000));
  const page = await ctx.newPage();
  return { ctx, page };
}

async function settle(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
}

async function household(page, value) {
  const current = await (
    await page.request.get(`${base}/admin/api/settings/prompt-memory`)
  ).json();
  const session = await (
    await page.request.get(`${base}/admin/api/session`)
  ).json();
  const response = await page.request.put(
    `${base}/admin/api/settings/prompt-memory`,
    {
      data: { ...current.prompt_memory, ...value },
      headers: {
        "if-match": current.revision,
        "x-azizam-csrf": session.actor.csrf_token,
      },
    },
  );
  if (!response.ok()) throw new Error(`household PUT ${response.status()}`);
}

const shots = {
  // Member: their own settings.
  "account-member": { as: seed.member_email, go: "/ui/account/" },
  // Member: Try with the default (kept) and with Don't keep on.
  "try-kept": { as: seed.member_email, go: "/ui/try/?capability=image" },
  "try-forget": {
    as: seed.member_email,
    go: "/ui/try/?capability=image",
    act: async (page) => {
      await page
        .getByRole("switch", { name: "Don’t keep this prompt" })
        .click();
    },
  },
  // Library: the maker's own item, the maker's not-kept item, and the
  // owner viewing the same member's item.
  "library-maker": {
    as: seed.member_email,
    go: `/ui/library/?asset=${kept.asset_id}`,
    act: async (page) => {
      await page.getByRole("dialog").waitFor();
      await page.getByRole("button", { name: /^Actions for / }).click();
    },
  },
  "library-maker-not-saved": {
    as: seed.member_email,
    go: `/ui/library/?asset=${notKept.asset_id}`,
    act: async (page) => page.getByRole("dialog").waitFor(),
  },
  "library-owner-sees-member": {
    as: seed.owner_email,
    go: `/ui/library/?asset=${kept.asset_id}`,
    act: async (page) => page.getByRole("dialog").waitFor(),
  },
  "library-owner-own": {
    as: seed.owner_email,
    go: `/ui/library/?asset=${samKept.asset_id}`,
    act: async (page) => page.getByRole("dialog").waitFor(),
  },
  // Owner: the household's rules.
  "settings-owner": {
    as: seed.owner_email,
    go: "/ui/settings/",
    act: async (page) => {
      // The Prompts heading just under the header, with Defaults above it.
      await page
        .locator("#prompts")
        .evaluate((el) =>
          window.scrollTo(
            0,
            el.getBoundingClientRect().top + window.scrollY - 120,
          ),
        );
    },
  },
  // Overrides, as a member sees them.
  "account-capped": {
    as: seed.member_email,
    owner: { retention_cap: "3_months" },
    go: "/ui/account/",
  },
  // Background cleanup still running after Delete my saved prompts.
  "account-cleanup": {
    as: seed.member_email,
    go: "/ui/account/",
    route: async (page) => {
      await page.route("**/me/api/prompt-memory", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.prompt_purge = {
          pending: true,
          metadata_stripping: { status: "queued", files_pending: 12 },
        };
        await route.fulfill({ response, json: body });
      });
    },
  },
  // A new key on the member's own app.
  "apps-key": {
    as: seed.member_email,
    go: "/ui/apps/",
    act: async (page) => {
      await page.getByRole("button", { name: "New app" }).first().click();
      await page.getByLabel("App name").fill("Kitchen tablet");
      await page.getByRole("button", { name: "Create app and key" }).click();
      await page
        .getByRole("heading", { name: "Connect Kitchen tablet" })
        .waitFor();
    },
  },
  // Last: the kill switch deletes every saved prompt in the fixture.
  "account-household-off": {
    as: seed.member_email,
    owner: { never_keep: true },
    go: "/ui/account/",
  },
  "try-household-off": {
    as: seed.member_email,
    owner: { never_keep: true },
    go: "/ui/try/?capability=image",
  },
};

const ownerCtx = await context(seed.owner_email, "light", sizes[1]);
for (const [name, shot] of Object.entries(shots)) {
  if (only && !name.includes(only)) continue;
  for (const theme of ["light", "dark"]) {
    for (const size of sizes) {
      if (shot.owner) await household(ownerCtx.page, shot.owner);
      const { ctx, page } = await context(shot.as, theme, size);
      try {
        if (shot.route) await shot.route(page);
        await page.goto(`${base}${shot.go}`);
        await settle(page);
        if (shot.act) await shot.act(page);
        await settle(page);
        await page.screenshot({
          path: path.join(outDir, `${name}-${size.name}-${theme}.png`),
          // Viewport only: the phone tab bar is fixed (ban 31).
          fullPage: false,
        });
      } catch (error) {
        console.error(`${name} ${size.name} ${theme}: ${error.message}`);
      } finally {
        await ctx.close();
        if (shot.owner)
          await household(ownerCtx.page, {
            never_keep: false,
            retention_cap: "never",
            default_remember: true,
          });
      }
    }
  }
}
await ownerCtx.ctx.close();
await browser.close();
