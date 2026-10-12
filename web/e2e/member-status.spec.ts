import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// The Home pass-through route can still be fetching when a test ends.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("member Status offers sign-in when its account snapshot expires", async ({
  page,
}) => {
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
          id: "expired-member",
          name: "Member",
          role: "member",
          email: null,
        },
      },
    }),
  );
  await page.route("**/me/api/status", (route) =>
    route.fulfill({
      status: 401,
      json: { error: { code: "unauthorized", message: "Session expired" } },
    }),
  );
  await page.goto("/status/");
  await expect(
    page.getByText("Sign in to see your status", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toHaveAttribute("href", "/sign-in/?return_to=%2Fstatus%2F");
  expect(ownerCalls).toEqual([]);
});

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
        // Status reads the member's capability states from their Home
        // snapshot (the shared model), so Home carries the same rows.
        const checkedAt = Math.floor(Date.now() / 1000) - 120;
        const imageRow = {
          capability: "image",
          capability_title: "Images",
          icon_key: "image",
          title: "Flux",
          recommended_model: "Flux",
          use_names: ["image", "flux"],
          summary: "Generate images with Flux.",
          intent: "image.generate",
          model: "image-model",
          workflow: null,
          available: false,
          status: "unavailable",
          reason_code: "unavailable",
        };
        await page.route("**/me/api/home", async (route) => {
          const response = await route.fetch();
          const home = await response.json();
          return route.fulfill({
            response,
            json: {
              ...home,
              capabilities:
                state === "empty"
                  ? []
                  : [
                      {
                        ...imageRow,
                        availability: {
                          evidence: {
                            source: "status",
                            observed_at_s: checkedAt,
                          },
                        },
                      },
                    ],
              aliases: [],
              receipts: [],
            },
          });
        });
        let failed = state === "error";
        await page.route("**/me/api/status", (route) =>
          failed
            ? route.fulfill({
                status: 503,
                json: { error: { message: "Unavailable" } },
              })
            : route.fulfill({
                json: {
                  scope: "member",
                  status: {
                    state: "unavailable",
                    reason_code:
                      state === "empty" ? "no_capabilities" : "unavailable",
                    affected_capability_ids: state === "empty" ? [] : ["image"],
                    counts:
                      state === "empty"
                        ? { total: 0, available: 0, affected: 0 }
                        : { total: 1, available: 0, affected: 1 },
                  },
                  capabilities:
                    state === "empty"
                      ? []
                      : [
                          {
                            ...imageRow,
                            measured: true,
                            observed_at_s: checkedAt,
                          },
                        ],
                  receipts:
                    state === "empty"
                      ? []
                      : [
                          {
                            request_id: "member-status-receipt",
                            status: "failed",
                            intent: "image.generate",
                            created_at: 1790870400,
                            href: "/usage/?receipt=member-status-receipt",
                          },
                        ],
                  help: "",
                },
              }),
        );
        const yourCapabilities = page.getByRole("region", {
          name: "What you can use",
        });
        await page.goto("/status/");
        if (state === "error") {
          await expect(
            page.getByRole("button", { name: "Try again" }),
          ).toBeVisible();
          // An HTTP answer proves only that the gateway answered.
          await expect(
            page.getByText("The gateway returned an error. Try again.", {
              exact: true,
            }),
          ).toBeVisible();
        } else if (state === "empty") {
          await expect(
            page.getByText("No models are available to your account yet", {
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            // Names the owner when the gateway knows their name.
            page.getByText(
              /^Ask (the gateway owner|[^ ]+) to connect a model and give you access\.$/,
            ),
          ).toBeVisible();
        } else
          await expect(
            yourCapabilities.getByText("Images", { exact: true }),
          ).toBeVisible();
        if (state === "seeded") {
          await expect(
            yourCapabilities
              .getByRole("listitem")
              .filter({ hasText: "Images" }),
          ).toContainText("Down");
          await expect(
            page.getByText(
              "Your app reached the gateway. Images is down now.",
              {
                exact: true,
              },
            ),
          ).toBeVisible();
        }
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
          fullPage: width !== 390,
        });
        if (state === "error") {
          failed = false;
          await page.getByRole("button", { name: "Try again" }).click();
          await expect(
            yourCapabilities.getByText("Images", { exact: true }),
          ).toBeVisible();
        }
        expect(ownerCalls).toEqual([]);
      });
    }
  }
}
