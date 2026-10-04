#!/usr/bin/env node
// Capture the gate's real-data pages using sessions created only on the copy.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../../..",
);
const runtime = path.join(root, ".ui-e2e-state/gate-pass2/runtime");
const out =
  process.env.GATE_PASS2_CAPTURE_DIR ??
  "/home/tnunamak/.tmp/gw-product/gate/v5";
const base = "http://127.0.0.1:18531";
const cookies = JSON.parse(
  fs.readFileSync(path.join(runtime, "session-cookies.json"), "utf8"),
);
const clean = (value) =>
  String(value).replace(/(?:oak_|opk_)[\w-]+/g, "[REDACTED]");
const browser = await chromium.launch({
  executablePath: "/usr/bin/google-chrome",
  headless: true,
});
const records = [];
const contracts = [];
fs.mkdirSync(out, { recursive: true });
try {
  for (const role of ["owner", "member"]) {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({
        baseURL: base,
        colorScheme: theme,
      });
      await context.addCookies([cookies[role]]);
      await context.addInitScript(
        (theme) => localStorage.setItem("theme", theme),
        theme,
      );
      if (theme === "light") {
        const facts = { role };
        for (const [name, route] of [
          ["home", "/me/api/home"],
          ["status", "/me/api/status"],
          ["models", "/me/api/models/catalog"],
        ]) {
          const response = await context.request.get(route);
          if (!response.ok())
            throw new Error(`${role} ${route}: HTTP ${response.status()}`);
          const data = await response.json();
          const rows =
            name === "models"
              ? [...data.aliases, ...data.models]
              : data.capabilities;
          if (!rows.length)
            throw new Error(`${role} ${name}: empty copied-data projection`);
          if (
            name !== "models" &&
            new Set(rows.map((row) => row.capability)).size !== rows.length
          )
            throw new Error(`${role} ${name}: duplicate capability rows`);
          facts[name] =
            name === "models"
              ? rows.map((row) => ({
                  id: row.id,
                  title: row.title,
                  use_names: row.use_names,
                  engine_label: row.engine_label,
                  secondary_profile: row.secondary_profile,
                }))
              : {
                  summary: data.status,
                  rows: rows.map((row) => ({
                    capability: row.capability,
                    capability_title: row.capability_title,
                    recommended_model: row.recommended_model,
                    icon_key: row.icon_key,
                    use_names: row.use_names,
                  })),
                };
          if (rows.some((row) => !row.icon_key || !row.title))
            throw new Error(`${role} ${name}: presentation fields missing`);
          if (
            name !== "models" &&
            rows.some(
              (row) => row.capability === "music" && row.icon_key !== "music",
            )
          )
            throw new Error("Music icon taxonomy mismatch");
        }
        contracts.push(facts);
      }
      for (const [width, height] of [
        [1440, 900],
        [390, 844],
      ]) {
        for (const surface of ["status", "models", "home"]) {
          const page = await context.newPage();
          await page.setViewportSize({ width, height });
          const errors = [];
          page.on("pageerror", (error) => errors.push(clean(error.message)));
          page.on("console", (message) => {
            if (message.type() === "error") errors.push(clean(message.text()));
          });
          await page.goto(`/ui/${surface}/`);
          await page.waitForLoadState("networkidle");
          await page
            .locator(
              '[aria-busy="true"]:visible, [data-slot="skeleton"]:visible',
            )
            .first()
            .waitFor({ state: "hidden", timeout: 20000 });
          await page.evaluate(() => document.fonts.ready);
          const overflow = await page.evaluate(
            () =>
              Math.max(
                document.documentElement.scrollWidth,
                document.body.scrollWidth,
              ) - innerWidth,
          );
          const axe = await new AxeBuilder({ page }).analyze();
          const serious = axe.violations
            .filter((row) => ["serious", "critical"].includes(row.impact))
            .map((row) => row.id);
          const stem = `${role}-${surface}-${width === 390 ? "phone" : "desktop"}-${theme}`;
          await page.screenshot({ path: path.join(out, `${stem}.png`) });
          await page.screenshot({
            path: path.join(out, `${stem}-full.png`),
            fullPage: true,
          });
          records.push({
            role,
            surface,
            width,
            theme,
            overflow,
            serious,
            errors,
            text: clean(await page.locator("main").innerText()),
          });
          await page.close();
        }
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
  fs.writeFileSync(
    path.join(out, "verification.json"),
    JSON.stringify({ contracts, records }, null, 2),
  );
}
const failed = records.filter(
  (row) => row.overflow > 0 || row.serious.length || row.errors.length,
);
console.log(
  JSON.stringify({
    captures: records.length,
    screenshots: records.length * 2,
    failed: failed.map(
      ({ role, surface, width, theme, overflow, serious, errors }) => ({
        role,
        surface,
        width,
        theme,
        overflow,
        serious,
        errors,
      }),
    ),
  }),
);
if (failed.length) process.exitCode = 1;
