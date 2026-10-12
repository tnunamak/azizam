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
