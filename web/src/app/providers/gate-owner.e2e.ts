import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const theme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"]) {
      test(`Providers ${state} ${theme} ${width}`, async ({ page }) => {
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
        await page.route("**/admin/api/profiles", (route) =>
          route.fulfill({ json: { profiles: [] } }),
        );
        await page.route("**/admin/api/providers**", (route) => {
          if (new URL(route.request().url()).pathname.endsWith("/catalog"))
            return route.fulfill({ json: { providers: [] } });
          if (state === "error")
            return route.fulfill({
              status: 500,
              json: { error: { message: "Provider status failed." } },
            });
          return route.fulfill({
            json: {
              providers:
                state === "empty"
                  ? []
                  : [
                      {
                        id: "remote-openai",
                        name: "OpenAI",
                        enabled: true,
                        reachable: true,
                        models: [
                          {
                            id: "openai/gpt-4.1-nano",
                            name: "Gpt 4.1 Nano",
                            enabled: true,
                            input_price_per_million: 0.1,
                            output_price_per_million: 0.4,
                          },
                        ],
                        spend: { daily_usd: 0, monthly_usd: 0 },
                        caps: { daily_usd: 5, monthly_usd: 20 },
                        allowed_plans: [],
                      },
                    ],
              global: {
                spend: { daily_usd: 0, monthly_usd: 0 },
                caps: { daily_usd: 5, monthly_usd: 20 },
              },
            },
          });
        });
        await page.goto("/ui/providers/");
        await expect(
          page.getByRole("heading", { name: "Providers", exact: true }),
        ).toBeVisible();
        if (state === "seeded") {
          await expect(
            page.getByRole("cell", {
              name: "openai/gpt-4.1-nano",
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            page.getByText("Gpt 4.1 Nano", { exact: true }),
          ).toHaveCount(0);
          await expect(
            page.getByText("Provider ID: remote-openai"),
          ).toBeHidden();
          await page.getByText("Provider details", { exact: true }).click();
          await expect(
            page.getByText("Provider ID: remote-openai"),
          ).toBeVisible();
          await page.getByText("Provider details", { exact: true }).click();
        } else if (state === "empty") {
          await expect(
            page.getByText("No providers yet", { exact: true }),
          ).toBeVisible();
        } else {
          await expect(
            page.getByRole("button", { name: "Retry providers" }),
          ).toBeVisible();
        }
        await expect(
          page.getByText("Plans allowed by configured providers", {
            exact: true,
          }),
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
          path: `test-results/screens/gate-owner-providers-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
      });
    }
  }
}
