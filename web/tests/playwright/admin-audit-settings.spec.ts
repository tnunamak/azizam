import { expect, test } from "@playwright/test";

const oidcConfig = {
  enabled: true,
  issuer: "https://idp.example.test",
  client_id: "gateway-admin",
  client_secret: "<redacted>",
  client_secret_set: true,
  redirect_uri: "https://gateway.example.test/admin/oidc/callback",
  scopes: ["openid", "email"],
  allowed_email_domains: ["example.test"],
  admin_emails: ["owner@example.test"],
  admin_groups: ["operators"],
  admin_roles: ["admin"],
  authorization_endpoint: "https://idp.example.test/authorize",
  token_endpoint: "https://idp.example.test/token",
  jwks_uri: "https://idp.example.test/jwks",
  clock_skew_seconds: 60,
  groups_claim: "groups",
  updated_at: 1_770_000_000,
  source: "merged",
  apply_mode: "live",
  provenance: {
    summary: "merged",
    fields: { issuer: "bootstrap", client_id: "database" },
  },
};

async function mockAdminApi(page: import("@playwright/test").Page) {
  await page.route("**/admin/api/session", async (route) =>
    route.fulfill({
      json: {
        actor: {
          subject: "opaque-actor-id",
          email: "operator@example.test",
          roles: ["owner"],
          csrf_token: "test-csrf-token",
        },
      },
    }),
  );
  await page.route("**/admin/api/audit**", async (route) =>
    route.fulfill({
      json: {
        records: [
          {
            id: "aud_test_1",
            created_at: 1_770_000_000,
            actor_kind: "service_account",
            actor_id: "opaque-actor-id",
            actor_label: "Release bot",
            action: "operator.oidc_provider_config.update",
            target_kind: "operator_setting",
            target_id: "oidc_provider_config",
            metadata: null,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/admin/api/oidc-config", async (route) => {
    if (route.request().method() === "PUT") {
      const request = route.request().postDataJSON() as Record<string, unknown>;
      expect(request.client_secret).toBe("rotated-secret");
    }
    await route.fulfill({ json: { oidc_config: oidcConfig } });
  });
  await page.route("**/admin/api/extensions", async (route) =>
    route.fulfill({ json: { extension_packages: { packages: {} } } }),
  );
  await page.route("**/admin/api/operator/overrides", async (route) =>
    route.fulfill({ json: { overrides: { media_job_admission: null } } }),
  );
}

test("Audit and Settings mount their closed contract states", async ({
  page,
}) => {
  await mockAdminApi(page);
  await page.goto("/ui/admin/audit/");
  await expect(page.getByRole("heading", { name: "Audit" })).toBeVisible();
  await expect(page.getByText("Release bot", { exact: true })).toBeVisible();

  await page.goto("/ui/admin/settings/");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(
    page.getByText("merged configuration", { exact: true }),
  ).toBeVisible();
  const signInCard = page
    .getByRole("heading", { name: "Sign-in" })
    .locator("xpath=../..");
  await signInCard.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Client secret").fill("rotated-secret");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Sign-in settings saved")).toBeVisible();
});
