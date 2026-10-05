import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const generatedAsset = {
  asset_id: "asset_generated_image",
  request_id: "req_generated_image",
  kind: "image",
  prompt: "",
  model: "image",
  created_at: 1_760_000_000,
  download_url:
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' fill='%230f766e'/%3E%3Ccircle cx='26' cy='24' r='6' fill='white'/%3E%3C/svg%3E",
  width: 64,
  height: 64,
};

async function setViewportAndTheme(
  page: Page,
  mode: "light" | "dark",
  width: 1440 | 390,
) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  await page.emulateMedia({ colorScheme: mode, reducedMotion: "reduce" });
  await page.addInitScript(
    (value) => localStorage.setItem("theme", value),
    mode,
  );
}

async function auditPage(page: Page) {
  const violations = (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}

for (const mode of ["light", "dark"] as const) {
  for (const width of [1440, 390] as const) {
    test(`library receipt provenance ${mode} ${width}`, async ({ page }) => {
      await setViewportAndTheme(page, mode, width);
      // The media fixture points at the real request stored by the harness.
      const usage = await page.request.get("/me/api/usage/summary");
      expect(usage.ok()).toBe(true);
      const requestId: string = (await usage.json()).receipts[0].request_id;
      await page.route("**/me/api/media?**", (route) =>
        route.fulfill({
          json: {
            assets: [{ ...generatedAsset, request_id: requestId }],
            models: ["image"],
            next_cursor: null,
          },
        }),
      );

      await page.goto("/ui/library/");

      await expect(page.getByRole("main")).toHaveCount(1);
      await page
        .getByRole("button", { name: "Open Image" })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Image",
      });
      await expect(dialog.getByText("Prompt not recorded.")).toHaveCount(0);
      await dialog.getByText("Details").click();
      const details = dialog.locator("details");
      await expect(
        dialog.getByRole("link", { name: `View receipt ${requestId}` }),
      ).toHaveAttribute("href", `/r/${encodeURIComponent(requestId)}`);
      await expect(details.getByText(requestId)).toBeVisible();
      await auditPage(page);
      expect(
        await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
      ).toBe(true);
      await dialog
        .getByRole("link", { name: `View receipt ${requestId}` })
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `test-results/screens/library-batch-j-${mode}-${width}.png`,
      });
      const receiptResponse = page.waitForResponse((response) =>
        response.url().endsWith(`/me/api/receipts/${requestId}`),
      );
      await dialog
        .getByRole("link", { name: `View receipt ${requestId}` })
        .click();
      await expect(page).toHaveURL(new RegExp(`/r/${requestId}$`));
      const receipt = await receiptResponse;
      expect(receipt.status()).toBe(200);
      expect((await receipt.json()).request_id).toBe(requestId);
      await expect(page.locator("#receipt-sheet")).toBeVisible();
      await expect(page.locator("#receipt-body")).toContainText(requestId);
    });
  }
}
