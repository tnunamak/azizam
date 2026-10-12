import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

const screenshots = "/home/tnunamak/.tmp/gw-product/ui-review/gw-copy-shared";

async function checkAccessibility(page: Page) {
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => ({
      id: violation.id,
      targets: violation.nodes.map((node) => node.target),
    }));
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`account loading and recovery: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      let release!: () => void;
      const pending = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route("**/me/api/session", async (route) => {
        await pending;
        await route.fulfill({
          status: 503,
          json: {
            error: {
              code: "service_unavailable",
              message: "Fixture account outage",
            },
          },
        });
      });
      await page.goto("/ui/models/");
      const loading = page.getByRole("status", {
        name: "Loading your account",
      });
      await expect(loading).toBeVisible();
      await expect(loading).toHaveAttribute("aria-busy", "true");
      // A session that has not answered is not signed out. Keep private
      // content and navigation unmounted until the account is known.
      await expect(page).toHaveURL(/\/ui\/models\/$/);
      await expect(page.getByRole("navigation")).toHaveCount(0);
      await expect(page.getByRole("heading", { name: "Models" })).toHaveCount(
        0,
      );
      await checkAccessibility(page);
      await mkdir(screenshots, { recursive: true });
      await page.screenshot({
        path: `${screenshots}/shared-loading-${theme}-${width}.png`,
        fullPage: true,
      });
      release();
      const failure = page
        .getByRole("alert")
        .filter({ hasText: "Couldn’t load your account" });
      await expect(failure).toBeVisible();
      await expect(failure).toContainText(
        "We couldn't tell why your account didn't load. Try again.",
      );
      await expect(page).toHaveURL(/\/ui\/models\/$/);
      await expect(page.getByRole("navigation")).toHaveCount(0);
      await checkAccessibility(page);
      await page.screenshot({
        path: `${screenshots}/shared-error-${theme}-${width}.png`,
        fullPage: true,
      });
      await page.unroute("**/me/api/session");
      await failure
        .getByRole("button", { name: "Try again", exact: true })
        .click();
      await expect(failure).toHaveCount(0);
      await expect(
        page.getByRole("heading", { name: "Models", exact: true, level: 1 }),
      ).toBeVisible();
      // A signed-in person sees the shared capability verdict, never the
      // signed-out "Gateway reachable" (DESIGN.md, Status model rule 3).
      await expect(
        page.getByRole("button", {
          name: /^Gateway status: (All working|\d+ issues?|Not checked yet|Busy|Nothing turned on|Status unknown)$/,
        }),
      ).toBeVisible();
      await checkAccessibility(page);
    });

    test(`shared copy and keyboard recovery: ${theme} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      // The design page uses a plainly fake key; this test never requests a real credential.
      await page.addInitScript(() => {
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: { writeText: async () => undefined },
        });
      });
      await page.goto("/ui/design/");
      const copy = page.getByRole("button", {
        name: "Copy gateway address",
        exact: true,
      });
      await copy.click();
      const copied = page
        .getByRole("status")
        .filter({ hasText: "Gateway address copied to clipboard." });
      await expect(copied).toHaveCount(1);
      await expect(copied).not.toContainText("https://gateway.example/v1");

      const keyCopy = page.getByRole("button", {
        name: "Copy your key",
        exact: true,
      });
      await keyCopy.click();
      const keyStatus = page
        .getByRole("status")
        .filter({ hasText: "Your key copied to clipboard." });
      await expect(keyStatus).toHaveCount(1);
      await expect(keyStatus).not.toContainText(
        "example-key-not-a-real-secret",
      );
      const confirmation = page.getByRole("button", {
        name: "I’ve saved it",
        exact: true,
      });
      if (width === 390) {
        for (const control of [copy, keyCopy, confirmation]) {
          const bounds = await control.boundingBox();
          expect(bounds?.height).toBeGreaterThanOrEqual(44);
          expect(bounds?.width).toBeGreaterThanOrEqual(44);
        }
      }
      await confirmation.click();
      await expect(
        page.getByText("Key hidden. It won’t be shown again.", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("textbox", { name: "Your key", exact: true }),
      ).toHaveCount(0);

      // Both overlays must restore focus to the initiating control on Escape.
      for (const [triggerName, dialogName] of [
        ["Open dialog", "Create an app"],
        ["Open sheet", "Request details"],
      ]) {
        const trigger = page.getByRole("button", {
          name: triggerName,
          exact: true,
        });
        await trigger.focus();
        await page.keyboard.press("Enter");
        const overlay = page.getByRole("dialog", {
          name: dialogName,
          exact: true,
        });
        await expect(overlay).toBeVisible();
        await checkAccessibility(page);
        await page.keyboard.press("Escape");
        await expect(overlay).not.toBeVisible();
        await expect(trigger).toBeFocused();
      }
      await checkAccessibility(page);
      await mkdir(screenshots, { recursive: true });
      await page.screenshot({
        path: `${screenshots}/shared-copy-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("denied clipboard announces recovery and preserves the exact selectable address", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => {
          throw new DOMException("Denied", "NotAllowedError");
        },
      },
    });
  });
  await page.goto("/ui/design/");
  await page
    .getByRole("button", { name: "Copy gateway address", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: /select.*copy.*manually/i }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Gateway address copied to clipboard." }),
  ).toHaveCount(0);
  const field = page.getByRole("textbox", {
    name: "Gateway address",
    exact: true,
  });
  await expect(field).toHaveValue("https://gateway.example/v1");
  await expect(field).toBeEnabled();
  const selected = await field.evaluate((element: HTMLInputElement) => {
    element.focus();
    element.select();
    return element.value.slice(element.selectionStart!, element.selectionEnd!);
  });
  expect(selected).toBe("https://gateway.example/v1");
});

test("global connection status describes reachability while engine diagnostics stay separate", async ({
  page,
}) => {
  await page.route("**/health", (route) =>
    route.fulfill({ json: { status: "ok" } }),
  );
  await page.route("**/admin/api/status", (route) =>
    route.fulfill({
      json: {
        status: {
          current_health: {
            state: "warn",
            tone: "warn",
            label: "Needs attention",
            detail: "",
            observation_available: true,
            services: [
              {
                service_id: "fixture-images",
                name: "Image engine",
                configured: true,
                enabled: true,
                in_use: true,
                role: "required",
                routable: false,
                observation: "down",
                tone: "bad",
                last_error: "Connection refused",
              },
            ],
          },
        },
      },
    }),
  );
  await page.goto("/ui/design/");
  const connection = page.getByRole("button", { name: /Gateway status:/ });
  await expect(connection).not.toHaveAccessibleName(
    /Degraded|Models unavailable|Down/,
  );
  await connection.click();
  await expect(page.getByLabel("Gateway health")).toContainText(
    /gateway.*reachable/i,
  );
});
