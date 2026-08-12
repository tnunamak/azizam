import { expect, test } from "@playwright/test";

const auditPage = {
  records: [
    {
      id: "aud_test_1",
      created_at: 1_770_000_000,
      actor_kind: "cli",
      actor_id: "cli",
      action: "app.create",
      target_kind: "app",
      target_id: "app_1",
      metadata: { name: "Example", token: "must-not-render" },
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
  await page.route("**/admin/api/apps", async (route) => {
    await route.fulfill({
      json: [
        {
          id: "app_1",
          name: "Example",
          kind: "shared",
          owner_user_id: null,
          effective_profile_id: null,
          effective_profile_name: "Default access",
          effective_profile_source: "fallback",
          disabled_at: null,
          created_at: 1_770_000_000,
        },
      ],
    });
  });
  await page.route("**/admin/api/credentials", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/admin/api/profiles", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/admin/api/session", async (route) => {
    await route.fulfill({
      json: {
        actor: { subject: "usr_test", email: null, roles: [], csrf_token: "" },
      },
    });
  });
  await page.route("**/admin/api/status", async (route) => {
    await route.fulfill({ json: { auth_keys: [] } });
  });
}

async function expectMinimumTarget(
  locator: import("@playwright/test").Locator,
) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.width).toBeGreaterThanOrEqual(44);
  expect(box!.height).toBeGreaterThanOrEqual(44);
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

test("Audit and Settings use 44px compact targets, restore density at 980px, and return to Audit", async ({
  page,
}) => {
  await mockAdminApi(page);

  for (const width of [640, 979]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(
      "/ui/admin/audit/?actor=usr_1&category=app&event=aud_test_1",
    );
    await expectMinimumTarget(page.getByRole("link", { name: "Example" }));
    await expectMinimumTarget(
      page.getByRole("button", { name: "Copy event JSON" }),
    );

    await page.goto("/ui/admin/settings/");
    await expectMinimumTarget(page.getByRole("button", { name: "Configure" }));
    await page.getByRole("button", { name: "Configure" }).click();
    await expectMinimumTarget(page.getByRole("button", { name: "Close" }));
    await expectMinimumTarget(
      page.getByRole("switch", { name: "Enable OIDC sign-in" }),
    );
    await expectMinimumTarget(page.getByText("Advanced options"));
    await expectMinimumTarget(page.getByRole("button", { name: "Cancel" }));
    await expectMinimumTarget(page.getByRole("button", { name: "Save" }));
  }

  await page.setViewportSize({ width: 980, height: 900 });
  await page.goto("/ui/admin/settings/");
  expect(
    (await page.getByRole("button", { name: "Configure" }).boundingBox())!
      .height,
  ).toBe(32);

  await page.goto("/ui/admin/audit/?actor=usr_1&category=app&event=aud_test_1");
  const target = page.getByRole("link", { name: "Example" });
  await expect(target).toHaveAttribute(
    "href",
    "/ui/admin/apps/?app=app_1&return=%2Fadmin%2Faudit%2F%3Factor%3Dusr_1%26category%3Dapp%26event%3Daud_test_1",
  );
  await target.click();
  await expect(page.getByRole("link", { name: "Back to audit" })).toBeVisible();
  await page.getByRole("link", { name: "Back to audit" }).click();
  await expect(page).toHaveURL(
    /\/ui\/admin\/audit\/\?actor=usr_1&category=app&event=aud_test_1$/,
  );
});
