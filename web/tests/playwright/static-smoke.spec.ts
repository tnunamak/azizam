import { expect, test } from "@playwright/test";

test("uses the global setup static fixture without a gateway", async ({
  page,
}) => {
  const fixtureUrl = process.env.VF_UI_STATIC_SMOKE_URL;
  expect(fixtureUrl).toBeTruthy();

  await page.goto(fixtureUrl!);

  await expect(page).toHaveTitle("Vivid Fish browser smoke");
  const toggle = page.getByRole("button", { name: "Toggle state" });
  await toggle.press("Space");
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
});
