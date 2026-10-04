import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

const repoRoot = path.resolve(__dirname, "../../../..");
const stateRoot = path.join(repoRoot, ".ui-e2e-state/status-real");
const screenshotRoot = "/home/tnunamak/.tmp/gw-product/ui-review/gw-fix-status";

test("member Status renders copied live-data projection without owner details", async ({
  page,
}) => {
  const memberStatus = JSON.parse(
    await readFile(path.join(stateRoot, "member-status.json"), "utf8"),
  );
  const ownerCalls: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
      ownerCalls.push(request.url());
    }
  });
  await page.route("**/me/api/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        apps: [],
        user: {
          id: "status-real-member",
          name: "Status real member",
          role: "member",
          email: "status-real-member@example.test",
        },
      },
    }),
  );
  await page.route("**/me/api/status", (route) =>
    route.fulfill({ json: memberStatus }),
  );

  for (const width of [1440, 390] as const) {
    await page.setViewportSize({
      width,
      height: width === 390 ? 844 : 1000,
    });
    await page.goto("/ui/status/");
    await expect(
      page.getByRole("heading", { name: "Status", level: 1 }),
    ).toBeVisible();
    await expect(page.locator('[aria-busy="true"]:visible')).toHaveCount(0);
    await expect(
      page.getByText("Your capabilities", { exact: true }),
    ).toBeVisible();
    expect(await page.locator("li").count()).toBeGreaterThan(0);
    await expect(
      page.getByText(/GPU|VRAM|Raw probe data|Providers|Engines/, {
        exact: true,
      }),
    ).toHaveCount(0);
    expect(ownerCalls).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    const serious = (await new AxeBuilder({ page }).analyze()).violations.filter(
      (violation) => ["serious", "critical"].includes(violation.impact ?? ""),
    );
    expect(serious).toEqual([]);
    await page.screenshot({
      path: path.join(screenshotRoot, `member-status-real-${width}.png`),
      fullPage: true,
    });
  }
});
