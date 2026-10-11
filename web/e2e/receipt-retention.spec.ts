import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";

// Seed only the test server's disposable database, never an operator file:
// it must be the one run directory under .ui-e2e-state serving this port.
function fixtureDatabase(port: number) {
  const root = path.resolve(__dirname, "../../.ui-e2e-state");
  const active = readdirSync(root)
    .filter((name) => name.startsWith("run-"))
    .map((name) => path.join(root, name))
    .filter(
      (directory) =>
        existsSync(path.join(directory, `${port}.log`)) &&
        readFileSync(path.join(directory, "config.yml"), "utf8").includes(
          `port: ${port}\n`,
        ),
    );
  if (active.length !== 1)
    throw new Error("Expected one disposable receipt fixture");
  return path.join(active[0], "receipts.db");
}

for (const width of [1440, 390]) {
  test(`owner previews and saves receipt expiry through the real API at ${width}px`, async ({
    page,
    baseURL,
  }) => {
    const db = fixtureDatabase(Number(new URL(baseURL!).port));
    const prefix = `retention-${width}-`;
    const now = Math.floor(Date.now() / 1000);
    for (const age of [40, 100, 400]) {
      const id = `${prefix}${age}`;
      const json = JSON.stringify({
        request_id: id,
        app_id: "receipt-fixture-app",
        status: "succeeded",
      });
      execFileSync("sqlite3", [
        db,
        `INSERT INTO receipts(request_id,created_at,app_id,status,receipt_json) VALUES('${id}',${now - age * 86400},'receipt-fixture-app','succeeded','${json}');`,
      ]);
    }
    const count = () =>
      Number(
        execFileSync(
          "sqlite3",
          [
            db,
            `SELECT COUNT(*) FROM receipts WHERE request_id LIKE '${prefix}%';`,
          ],
          { encoding: "utf8" },
        ).trim(),
      );
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/ui/settings/");
    const section = page.getByRole("region", { name: "Receipts", exact: true });
    const select = section.getByRole("combobox", { name: "Keep receipts" });
    await expect(select).toHaveText("Forever");
    await select.click();
    await page.getByRole("option", { name: "30 days", exact: true }).click();
    await expect(
      section.getByText(
        /Saving will permanently delete 3 receipts older than 30 days/,
      ),
    ).toBeVisible();
    expect(count()).toBe(3);
    await section.screenshot({
      path: `test-results/screens/receipt-preview-${width}.png`,
    });
    const a11y = await new AxeBuilder({ page })
      .include("section:has(#receipts)")
      .analyze();
    expect(
      a11y.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      ),
    ).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await select.click();
    await page.getByRole("option", { name: "Forever", exact: true }).click();
    await expect(
      section.getByRole("button", { name: "Save receipt setting" }),
    ).toBeDisabled();
    expect(count()).toBe(3);
    await select.click();
    await page.getByRole("option", { name: "30 days", exact: true }).click();
    await expect(
      section.getByText(/Saving will permanently delete 3 receipts/),
    ).toBeVisible();
    const saved = page.waitForResponse(
      (response) =>
        response.url().endsWith("/admin/api/settings/receipts") &&
        response.request().method() === "PUT",
    );
    await section.getByRole("button", { name: "Save receipt setting" }).click();
    const response = await saved;
    expect(response.status()).toBe(200);
    expect((await response.json()).deleted_count).toBe(3);
    expect(count()).toBe(0);
    await page.reload();
    await expect(select).toHaveText("30 days");
    await page.goto("/ui/usage/");
    await page.getByText("Receipt coverage", { exact: true }).click();
    await expect(
      page.getByText("Receipts are kept for 30 days."),
    ).toBeVisible();
    await page.goto("/ui/settings/");
    await select.click();
    await page.getByRole("option", { name: "Forever", exact: true }).click();
    await section.getByRole("button", { name: "Save receipt setting" }).click();
    await expect(section.getByText(/Saved: Forever/)).toBeVisible();
    await page.goto("/ui/usage/");
    await page.getByText("Receipt coverage", { exact: true }).click();
    await expect(
      page.getByText("Receipts are kept until you delete them."),
    ).toBeVisible();
  });
}
