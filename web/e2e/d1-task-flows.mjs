// Task-flow screenshots: the household's core jobs, step by step, as a person does them.
// Usage (from web/): D1_BASE=<url> D1_MANIFEST=<household manifest.json> node e2e/d1-task-flows.mjs <outdir>
// Needs a running household fixture (scripts/ui/serve.py with UI_SEED_PROFILE=household).
// Writes <flow>_<step>_<theme>_<width>.png plus manifest.json; one failed step does not stop the run.
import { chromium } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.env.D1_BASE;
const manifestPath = process.env.D1_MANIFEST;
const outdir = process.argv[2];
if (!base || !manifestPath || !outdir)
  throw new Error("Usage: D1_BASE=<url> D1_MANIFEST=<file> node e2e/d1-task-flows.mjs <outdir>");
const fixture = JSON.parse(await readFile(manifestPath, "utf8"));
await mkdir(outdir, { recursive: true });
const browser = await chromium.launch();
const results = [];
const cookies = {};

async function open(role, width, theme) {
  const context = await browser.newContext({
    viewport: { width, height: width < 640 ? 844 : 900 },
    colorScheme: theme,
    reducedMotion: "reduce",
    locale: "en-US",
    timezoneId: "America/Chicago",
  });
  await context.clock.setFixedTime(new Date(fixture.now * 1000));
  await context.addInitScript((value) => localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  if (cookies[role]) await context.addCookies(cookies[role]);
  else {
    await page.goto(`${base}/ui/sign-in/`);
    await page.getByLabel("Email", { exact: true }).fill(role === "owner" ? fixture.owner_email : fixture.member_email);
    await page.getByLabel("Password", { exact: true }).fill(fixture.password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.waitForURL((url) => !url.pathname.includes("/sign-in"), { timeout: 60_000 });
    cookies[role] = await context.cookies();
  }
  return { context, page };
}

async function settle(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(800);
}

/** Each flow is a list of [step name, action]; a screenshot follows every action. */
const flows = {
  // A member makes a picture from Home and finds it in Library.
  "make-a-picture": ["member", [
    ["home", async (page) => page.goto(`${base}/ui/home/`)],
    ["try", async (page) => page.getByRole("link", { name: /Make a picture/ }).first().click()],
    ["prompt", async (page) => {
      const prompt = page.getByRole("textbox", { name: /prompt/i }).first();
      await prompt.fill("A ripe tomato on a wooden table by a window");
    }],
    ["result", async (page) => {
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await page.waitForTimeout(4000);
    }],
    ["library", async (page) => page.goto(`${base}/ui/library/`)],
  ]],
  // A member has a sentence read aloud.
  "read-text-aloud": ["member", [
    ["home", async (page) => page.goto(`${base}/ui/home/`)],
    ["try", async (page) => page.getByRole("link", { name: /Read text aloud/ }).first().click()],
    ["result", async (page) => {
      const text = page.getByRole("textbox").first();
      await text.fill("Dinner is ready in ten minutes.");
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await page.waitForTimeout(4000);
    }],
  ]],
  // A member listens to a clip in Library.
  "play-a-clip": ["member", [
    ["library", async (page) => page.goto(`${base}/ui/library/`)],
    ["audio", async (page) => page.getByRole("group", { name: "Kind" }).getByRole("button", { name: "Audio", exact: true }).click()],
    ["viewer", async (page) => page.getByRole("button", { name: /^Open / }).first().click()],
  ]],
  // The owner finds what Robin made.
  "find-robins-work": ["owner", [
    ["library", async (page) => page.goto(`${base}/ui/library/`)],
    ["person", async (page) => {
      const picker = page.getByRole("combobox", { name: /Person|Everyone/ });
      if (await picker.isVisible().catch(() => false)) {
        await picker.click();
        await page.getByRole("option", { name: /Robin/ }).click();
      } else {
        await page.getByRole("button", { name: /^Filters/ }).click();
        await page.getByRole("combobox", { name: /Person|Everyone/ }).click();
        await page.getByRole("option", { name: /Robin/ }).click();
        await page.keyboard.press("Escape");
      }
    }],
  ]],
  // A member sees video is degraded on Home and follows it to Status.
  "why-is-video-degraded": ["member", [
    ["home", async (page) => page.goto(`${base}/ui/home/`)],
    ["status", async (page) => page.getByRole("link", { name: "See what is happening" }).click()],
  ]],
  // The owner goes from the problem to the engine.
  "fix-video": ["owner", [
    ["status", async (page) => page.goto(`${base}/ui/status/`)],
    ["inspect", async (page) =>
      page.getByRole("link", { name: /^Inspect / }).or(page.getByRole("button", { name: /^Inspect / })).first().click()],
  ]],
};

for (const [flow, [role, steps]] of Object.entries(flows)) {
  for (const theme of ["light"]) {
    for (const width of [390, 1440]) {
      const { context, page } = await open(role, width, theme);
      for (const [index, [step, action]] of steps.entries()) {
        const file = `${flow}_${index + 1}-${step}_${theme}_${width}.png`;
        try {
          await action(page);
          await settle(page);
          await page.screenshot({ path: path.join(outdir, file) });
          results.push({ flow, step, role, theme, width, file, url: page.url() });
        } catch (error) {
          results.push({ flow, step, role, theme, width, error: String(error).split("\n")[0] });
          break;
        }
      }
      await context.close();
    }
  }
}
await browser.close();
await writeFile(path.join(outdir, "manifest.json"), JSON.stringify(results, null, 2));
const failed = results.filter((item) => item.error);
console.log(`[flows] ${results.length - failed.length} screenshots, ${failed.length} failed steps`);
for (const item of failed) console.log(`  ${item.flow} ${item.step} ${item.width}: ${item.error}`);
