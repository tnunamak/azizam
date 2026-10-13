import { expect, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

test("Providers presents the current API shape without inventing readiness or names", async ({
  page,
}) => {
  await page.route("**/admin/api/providers", (route) =>
    fulfilJson(route, {
        global: {
          spend: { status: "unknown", monthly_usd: 888 },
        },
        providers: [
          {
            id: "internal-provider-record",
            name: "internal-provider-record",
            enabled: true,
            key_status: { state: "set" },
            spend: { status: "known", daily_usd: 1, monthly_usd: 12.5 },
            caps: { daily_usd: 2, monthly_usd: 50 },
          },
          {
            id: "missing-key",
            name: "Missing key provider",
            enabled: true,
            key_status: { state: "missing" },
            spend: { status: "unknown", monthly_usd: 999 },
          },
        ],
      }),
  );
  // Names, readiness words and spend rules are asserted in
  // src/app/providers/consistency.spec.tsx; the browser keeps phone geometry
  // and axe for the expanded rows in the dark theme.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("theme", "dark"));
  await page.goto("/providers/");
  const provider = page.locator("li").filter({
    has: page.getByRole("heading", { name: "Remote provider", exact: true }),
  });
  await provider.getByText("Manage", { exact: true }).click();
  await provider.getByText("Provider details", { exact: true }).click();
  const missing = page.locator("li").filter({
    has: page.getByRole("heading", {
      name: "Missing key provider",
      exact: true,
    }),
  });
  await missing.getByText("Manage", { exact: true }).click();
  await expect(missing.getByText("Cost not recorded")).toHaveCount(3);
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  expect(
    (await new AxeBuilder({ page }).analyze()).violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
});


test("legacy email names remain contact details on People and in search", async ({
  page,
}) => {
  const person = {
    id: "email-person",
    name: "person@example.test",
    email: "person@example.test",
    plan_id: null,
    plan_name: null,
    disabled_at: null,
    disabled: false,
    last_active: null,
    requests_this_month: null,
    builtin: true,
    protected: false,
    role: "member",
    grants: [],
  };
  await page.route("**/admin/api/people", (route) =>
    fulfilJson(route, { people: [person], access_plans: [] }),
  );
  await page.route("**/admin/api/users", (route) =>
    fulfilJson(route, { users: [{ ...person, display_name: person.email }] }),
  );
  await page.goto("/people/");
  await expect(page.getByText(person.email, { exact: true })).toHaveCount(1);
  await page.keyboard.press("Control+k");
  await page
    .getByRole("combobox", { name: "Go to" })
    .fill(person.email);
  const result = page.getByRole("option", {
    name: `Person ${person.email}`,
    exact: true,
  });
  await expect(result).toBeVisible();
  await expect(result.locator("span.text-muted-foreground")).toHaveText(
    person.email,
  );
});

test("personal access app hides only its exact generated email-based name", async ({
  page,
}) => {
  const email = "owner@example.test";
  const personalApp = {
    id: "app-personal",
    name: `${email} personal access`,
    kind: "personal",
    disabled_at: null,
    created_at: 1_760_000_000,
    profile: { name: "Default" },
    can_mint_keys: true,
    credentials: [],
  };
  const customApp = {
    ...personalApp,
    id: "app-custom",
    name: `${email} personal access app`,
  };
  await page.route("**/me/api/session", (route) =>
    fulfilJson(route, {
        authenticated: true,
        csrf_token: "test-csrf",
        user: { id: "owner", email, name: "", role: "owner" },
        apps: [personalApp, customApp],
      }),
  );
  await page.route("**/.well-known/azizam", (route) =>
    fulfilJson(route, { recipes: {} }),
  );
  await page.route("**/me/api/apps/app-personal/keys", (route) =>
    fulfilJson(route, { credential: { api_key: "test-key" } }),
  );

  await page.goto("/apps/");
  // Craft PR 3: each app is a row of the Apps ListCard.
  const card = page.getByRole("listitem").filter({
    has: page.getByRole("heading", { name: "Personal access", exact: true }),
  });
  await expect(
    card.getByRole("heading", { name: "Personal access", exact: true }),
  ).toBeVisible();
  await expect(card).not.toContainText(email);
  await expect(
    page.getByRole("heading", {
      name: `${email} personal access app`,
      exact: true,
    }),
  ).toBeVisible();
  await card
    .getByRole("button", { name: "Actions for Personal access" })
    .click();
  // APPS-01: an app with no keys offers "Create key"; "Create another key" is only for apps that have one.
  await page.getByRole("menuitem", { name: "Create key", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "Connect Personal access" }),
  ).toBeVisible();
});
