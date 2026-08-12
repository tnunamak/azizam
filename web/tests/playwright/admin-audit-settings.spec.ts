import { expect, test } from "@playwright/test";

const auditPage = {
  records: [
    {
      id: "aud_test_1",
      created_at: 1_770_000_000,
      actor_kind: "cli",
      actor_id: "cli",
      action: "operator.media_job_admission.update",
      target_kind: "operator_setting",
      target_id: "media_job_admission",
      metadata: { name: "Media admission", token: "must-not-render" },
    },
  ],
  next_cursor: null,
};

async function mockAdminApi(page: import("@playwright/test").Page) {
  await page.route("**/admin/api/audit**", async (route) => {
    await route.fulfill({ json: auditPage });
  });
  await page.route("**/admin/api/users", async (route) => {
    await route.fulfill({ json: { users: [] } });
  });
  await page.route("**/admin/api/oidc-config", async (route) => {
    await route.fulfill({ json: { oidc_config: null } });
  });
  await page.route("**/admin/api/extensions", async (route) => {
    await route.fulfill({ json: { extension_packages: { packages: {} } } });
  });
  await page.route("**/admin/api/operator/overrides", async (route) => {
    await route.fulfill({ json: { overrides: { media_job_admission: null } } });
  });
}

test("Audit and Settings retain dense, accessible layouts at route boundaries", async ({
  page,
}, testInfo) => {
  await mockAdminApi(page);

  for (const width of [360, 375, 390, 639, 640, 767, 768, 979, 980, 1280]) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto("/ui/admin/audit/");
    await expect(page.getByRole("heading", { name: "Audit" })).toBeVisible();
    await expect(page.getByText("System", { exact: true })).toBeVisible();
    expect(
      await page
        .locator("html")
        .evaluate((element) => element.scrollWidth <= element.clientWidth),
    ).toBe(true);
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/ui/admin/settings/");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(
    page.getByText("No dashboard OIDC override is saved."),
  ).toBeVisible();
  expect(
    await page
      .locator("html")
      .evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("admin-settings-390.png"),
    fullPage: true,
  });
});
