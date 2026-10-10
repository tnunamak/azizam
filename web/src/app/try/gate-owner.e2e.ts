import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const theme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"]) {
      test(`Try ${state} ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.emulateMedia({
          colorScheme: theme as "light" | "dark",
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (theme) => localStorage.setItem("theme", theme),
          theme,
        );
        await page.route("**/me/api/models/catalog", (route) =>
          route.fulfill({
            json: {
              aliases: [
                {
                  id: "decide",
                  name: "Decide",
                  capabilities: [{ intent: "decision.choice" }],
                  locality: "local",
                  cost: { kind: "free" },
                },
              ],
              models: [],
            },
          }),
        );
        await page.goto("/ui/try/?capability=decide");
        await expect(
          page.getByRole("heading", { name: "Try", exact: true }),
        ).toBeVisible();
        if (state !== "empty") {
          await page
            .getByRole("tab", { name: "Decisions", exact: true })
            .click();
          const id = "request-0123456789-abcdef0123456789";
          await page.route("**/me/api/receipts/**", (route) =>
            route.fulfill({ json: { request_id: id } }),
          );
          await page.route("**/v1/systemone", (route) =>
            route.fulfill(
              state === "error"
                ? {
                    status: 403,
                    json: {
                      error: {
                        message: "Your access plan does not allow decisions.",
                      },
                    },
                  }
                : {
                    headers: { "x-azizam-receipt-id": id },
                    json: {
                      answers: {
                        route: {
                          choice: "Take a taxi",
                          probabilities: {
                            "Take a taxi": 0.7,
                            "Wait for the bus": 0.3,
                          },
                        },
                      },
                    },
                  },
            ),
          );
          await expect(page.getByRole("combobox").first()).toContainText(
            "Local",
          );
          await page.getByRole("button", { name: "Run", exact: true }).click();
          if (state === "seeded") {
            await expect(
              page.getByText("Local · no provider charge", { exact: true }),
            ).toBeVisible();
            await expect(
              page.getByRole("link", { name: "Open receipt request-…" }),
            ).toHaveAttribute("href", `/r/${id}`);
            await page
              .context()
              .grantPermissions(["clipboard-read", "clipboard-write"]);
            await page.getByRole("button", { name: "Copy receipt ID" }).click();
            await expect
              .poll(() => page.evaluate(() => navigator.clipboard.readText()))
              .toBe(id);
            await expect(
              page.getByText("Receipt ID copied", { exact: true }),
            ).toHaveCount(1);
          } else {
            await expect(
              page.getByText("Your access plan does not allow decisions."),
            ).toBeVisible();
          }
        }
        await expect(
          page.getByText("Catalog loaded", { exact: true }),
        ).toHaveCount(0);
        await expect(
          page.getByText("Session key ready", { exact: true }),
        ).toHaveCount(0);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        const axe = await new AxeBuilder({ page }).analyze();
        expect(
          axe.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
        await page.screenshot({
          path: `test-results/screens/gate-owner-try-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
      });
    }
  }
}

test("Try prepares a key and links a real public chat receipt", async ({
  page,
}) => {
  await page.goto("/ui/try/");
  const chatResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/openai/v1/chat/completions") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  const response = await chatResponse;
  expect(response.ok()).toBe(true);
  const id = response.headers()["x-azizam-receipt-id"];
  expect(id).toBeTruthy();
  expect(response.headers()["x-azizam-request-id"]).toBe(id);
  await expect(page.getByText("Chat result", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "Your gateway is connected. This is a demonstration response from the isolated capture engine.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.locator(`a[href="/r/${id}"]`)).toBeVisible();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "Copy receipt ID" }).click();
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toBe(id);
  await expect(
    page.getByText("Session key ready", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("Catalog loaded", { exact: true })).toHaveCount(
    0,
  );
});
