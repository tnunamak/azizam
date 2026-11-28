import { expect, type Page } from "@playwright/test";

/** The old per-person and per-app rules sit in a closed section under the plain category list. */
export async function openAdvanced(page: Page) {
  const summary = page.locator('details > summary:text-is("Advanced: rules for specific people and apps")');
  await expect(summary).toBeVisible();
  const open = await summary.evaluate((node) => (node.parentElement as HTMLDetailsElement).open);
  if (!open) await summary.click();
}
