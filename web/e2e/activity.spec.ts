import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`activity requests ${state}: ${theme} ${width}`, async ({
        page,
      }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        if (state !== "seeded") {
          await page.route("**/admin/api/activity/requests?*", (route) =>
            route.fulfill({
              status: state === "error" ? 503 : 200,
              contentType: "application/json",
              body: JSON.stringify(
                state === "empty"
                  ? { records: [], next_cursor: null, people: [] }
                  : { error: { message: "Unavailable" } },
              ),
            }),
          );
        }
        await page.goto("/ui/activity/");
        await page.getByRole("tab", { name: "Requests", exact: true }).click();
        if (state === "seeded") {
          await expect(
            page.getByRole("button", { name: /Request details:/ }).first(),
          ).toBeVisible();
          await page
            .getByRole("button", { name: /Request details:/ })
            .first()
            .click();
          await expect(page.getByRole("dialog")).toBeVisible();
          await expect(
            page.getByRole("dialog").getByText("Completed", { exact: false }),
          ).toBeVisible();
          await page
            .getByRole("dialog")
            .getByText("Technical details", { exact: true })
            .click();
          await expect(page.getByRole("dialog").locator("pre")).toBeVisible();
          expect(
            await page
              .getByRole("dialog")
              .evaluate(
                (element) => element.scrollWidth <= element.clientWidth,
              ),
          ).toBe(true);
          await page.screenshot({
            path: `test-results/screens/activity-request-detail-${theme}-${width}.png`,
            fullPage: true,
          });
          const detailAxe = await new AxeBuilder({ page }).analyze();
          expect(
            detailAxe.violations.filter((v) =>
              ["serious", "critical"].includes(v.impact ?? ""),
            ),
          ).toEqual([]);
          await page.keyboard.press("Escape");
          const options = page.locator("#request-person option");
          expect(await options.count()).toBeGreaterThan(1);
          await page.locator("#request-person").selectOption({ index: 1 });
          await expect(
            page.getByRole("button", { name: /Request details:/ }).first(),
          ).toBeVisible();
          await page.getByRole("button", { name: "Clear filters" }).click();
        } else {
          await expect(
            page.getByText(
              state === "empty"
                ? "No requests yet"
                : "Requests could not be loaded. Try again.",
              { exact: true },
            ),
          ).toBeVisible();
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/activity-requests-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
      });
    }
  }
}

test("request errors retry and audit details use readable timestamps", async ({
  page,
}) => {
  let fail = true;
  await page.route("**/admin/api/activity/requests?*", (route) =>
    fail ? route.fulfill({ status: 503, body: "{}" }) : route.continue(),
  );
  await page.goto("/ui/activity/");
  await page
    .getByRole("button", { name: /Details:/ })
    .first()
    .click();
  await page
    .getByRole("dialog")
    .getByText("Technical details", { exact: true })
    .click();
  await expect(page.getByRole("dialog").locator("pre")).toContainText(
    /"created_at": "\d{4}-\d{2}-\d{2}T/,
  );
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Requests", exact: true }).click();
  await expect(
    page.getByText("Requests could not be loaded. Try again.", { exact: true }),
  ).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: /Retry|Try again/ }).click();
  await expect(
    page.getByRole("button", { name: /Request details:/ }).first(),
  ).toBeVisible();
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`activity audit ${state}: ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        if (state !== "seeded") {
          await page.route("**/admin/api/audit?*", (route) =>
            route.fulfill({
              status: state === "error" ? 503 : 200,
              contentType: "application/json",
              body: JSON.stringify(
                state === "empty"
                  ? { records: [], next_cursor: null }
                  : { error: { code: "unavailable", message: "Unavailable" } },
              ),
            }),
          );
        }
        await page.goto("/ui/activity/");
        if (state === "seeded")
          await expect(
            page.getByRole("button", { name: /Details:/ }).first(),
          ).toBeVisible();
        else
          await expect(
            page.getByText(
              state === "empty"
                ? "No changes yet"
                : "Audit events could not be loaded. Try again.",
              { exact: true },
            ),
          ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/activity-audit-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
      });
    }
  }
}
