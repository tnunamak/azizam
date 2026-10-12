import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`design overlays: ${theme} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(
        (value) => localStorage.setItem("theme", value),
        theme,
      );
      await page.goto("/design/");
      for (const [triggerName, dialogName] of [
        ["Open dialog", "Create an app"],
        ["Open sheet", "Request details"],
      ]) {
        const trigger = page.getByRole("button", {
          name: triggerName,
          exact: true,
        });
        await trigger.click();
        const overlay = page.getByRole("dialog", {
          name: dialogName,
          exact: true,
        });
        await expect(overlay).toBeVisible();
        const violations = (await new AxeBuilder({ page }).analyze()).violations
          .filter((v) => ["serious", "critical"].includes(v.impact ?? ""))
          .map((v) => ({ id: v.id, targets: v.nodes.map((n) => n.target) }));
        expect(violations).toEqual([]);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: `test-results/screens/design-${triggerName.replace("Open ", "")}-${theme}-${width}.png`,
          fullPage: true,
        });
        await page.keyboard.press("Escape");
        await expect(overlay).not.toBeVisible();
        await expect(trigger).toBeFocused();
      }
      await page
        .getByRole("button", { name: "App actions", exact: true })
        .click();
      await expect(
        page.getByRole("menuitem", { name: "Rotate unavailable key" }),
      ).toBeDisabled();
      await page.keyboard.press("Escape");
      await page
        .getByRole("button", { name: "Open popover", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Stable aliases" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
    });
  }
}

test("design demonstrations use capability and app vocabulary", async ({ page }) => {
  await page.goto("/ui/design/");
  for (const state of ["Default", "Disabled", "Error"]) {
    await expect(
      page.getByRole("combobox", { name: `${state} capability`, exact: true }),
    ).toContainText("Choose a capability");
  }
  await expect(
    page.getByText("Choose a model", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("combobox", { name: "Disabled capability", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("combobox", { name: "Default capability", exact: true })
    .click();
  await page.getByRole("option", { name: "Chat", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Default capability", exact: true }),
  ).toContainText("Chat");
  await expect(
    page.getByRole("columnheader", { name: "Capability", exact: true }),
  ).toHaveCount(2);
  await expect(
    page.getByRole("columnheader", { name: "Model", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Open dialog", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Give each app its own key and access plan.",
  );
  await page.keyboard.press("Escape");
});

test("design errors identify their layer, action and demonstration status", async ({
  page,
}) => {
  await page.goto("/ui/design/");
  await expect(
    page.getByRole("heading", { name: "Combobox · error demonstration" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Capabilities could not be loaded from the gateway. Reload this page.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText(
      "The gateway endpoint could not be loaded. Reload this page.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Copy field · demonstration" }),
  ).toBeVisible();
  await expect(page.getByText(/Check your connection/)).toHaveCount(0);

  const requestDemo = page
    .getByRole("heading", {
      name: "Request list error demonstration",
      exact: true,
    })
    .locator("..");
  await expect(requestDemo).toContainText(
    "The request list could not be loaded from the gateway. Try again.",
  );
  const retry = requestDemo.getByRole("button");
  await retry.click();
  await expect(retry).toBeDisabled();
  await expect(retry).toBeEnabled();

  await page.getByRole("tab", { name: "Error", exact: true }).click();
  const tab = page.getByRole("tabpanel", { name: "Error", exact: true });
  await expect(
    tab.getByRole("heading", { name: "Error demonstration", exact: true }),
  ).toBeVisible();
  await expect(tab).toContainText(
    "Requests could not be loaded from the gateway. Try again.",
  );

  const tableDemo = page
    .getByRole("heading", {
      name: "Table error demonstration",
      exact: true,
    })
    .locator("..");
  await expect(tableDemo).toContainText(
    "The request table could not be loaded from the gateway. Try again.",
  );
  await tableDemo
    .getByRole("button", { name: "Try again", exact: true })
    .click();
  await expect(
    page.getByText("Demonstration: retrying the request table.", { exact: true }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Error", exact: true }).click();
  await expect(
    page.getByText(
      "Demonstration: The gateway could not save these changes. Try again.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "App actions", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Revoke key", exact: true })
    .click();
  await expect(
    page.getByText(
      "Demonstration: The gateway could not revoke this key. Try again.",
      { exact: true },
    ),
  ).toBeVisible();
});
