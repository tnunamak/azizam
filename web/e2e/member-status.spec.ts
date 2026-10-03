import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    for (const state of ["seeded", "empty", "error"] as const) {
      test(`member Status ${state} ${theme} ${width}`, async ({ page }) => {
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 1000,
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        const ownerCalls: string[] = [];
        page.on("request", (request) => {
          if (new URL(request.url()).pathname.startsWith("/admin/api/"))
            ownerCalls.push(request.url());
        });
        await page.route("**/me/api/session", (route) =>
          route.fulfill({
            json: {
              authenticated: true,
              apps: [],
              user: {
                id: "member-status",
                name: "Member",
                role: "member",
                email: null,
              },
            },
          }),
        );
        let failed = state === "error";
        await page.route("**/me/api/home", (route) =>
          failed
            ? route.fulfill({
                status: 503,
                json: { error: { message: "Unavailable" } },
              })
            : route.fulfill({
                json: {
                  authenticated: true,
                  status: {
                    state: "unavailable",
                    reason: "Images are unavailable right now.",
                  },
                  capabilities:
                    state === "empty"
                      ? []
                      : [
                          {
                            capability: "image",
                            intent: "image.generate",
                            available: false,
                            status: "unavailable",
                            reason: "The image model is unavailable right now.",
                          },
                        ],
                },
              }),
        );
        await page.goto("/ui/status/");
        if (state === "error")
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toBeVisible();
        else if (state === "empty")
          await expect(
            page.getByText("No models are available to your account yet", {
              exact: true,
            }),
          ).toBeVisible();
        else
          await expect(
            page.getByRole("heading", { name: "Images" }),
          ).toBeVisible();
        await expect(page.locator('[aria-busy="true"]:visible')).toHaveCount(0);
        await page.evaluate(() => document.fonts.ready);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        expect(
          (await new AxeBuilder({ page }).analyze()).violations.filter((v) =>
            ["serious", "critical"].includes(v.impact ?? ""),
          ),
        ).toEqual([]);
        await expect(
          page.getByText(/GPU|disk|VRAM|Engines|Providers/, { exact: true }),
        ).toHaveCount(0);
        await page.screenshot({
          path: `test-results/screens/member-status-${state}-${theme}-${width}.png`,
          fullPage: true,
        });
        if (state === "error") {
          failed = false;
          await page.getByRole("button", { name: "Try again" }).click();
          await expect(
            page.getByRole("heading", { name: "Images" }),
          ).toBeVisible();
        }
        expect(ownerCalls).toEqual([]);
      });
    }
  }
}
