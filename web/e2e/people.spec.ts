import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const plan = {
  id: "plan-local",
  name: "Local creative",
  description: "For family and friends",
  created_at: 1720000000,
  protocols: ["openai"],
  workflows: [],
  status_visibility: "full",
  profile: {
    allow: { intents: ["chat", "images"], exposure: ["local"] },
    models: ["demo-chat"],
    fallback: { cloud_allowed: false },
    custom_operator_field: { preserve: true },
  },
};
const person = {
  id: "person-family",
  name: "Morgan Rivera",
  email: "morgan@example.test",
  profile_id: plan.id,
  plan_name: plan.name,
  disabled: false,
  last_active: Math.floor(Date.now() / 1000) - 3600,
  requests_this_month: null,
};
const invitation = {
  id: "invitation-family",
  email: "sam@example.test",
  profile_id: plan.id,
  plan_name: plan.name,
  expires_at: Math.floor(Date.now() / 1000) + 86400,
  created_at: Math.floor(Date.now() / 1000) - 3600,
  used_at: null,
  revoked_at: null,
};
const signIn = {
  built_in: true,
  oidc: true,
  oidc_label: "Example ID",
  oidc_url: "/admin/login?next=/ui/",
  instance_name: "Rivera household",
  owner_name: "Alex",
};
const editor = {
  schema_version: 1,
  profile: {
    create: { profile: {} },
    set_semantics: { omitted: "no_opinion", empty: "deny_all" },
    choices: {
      intents: [
        { id: "chat", label: "Chat" },
        { id: "images", label: "Images" },
      ],
      exposure: [
        { id: "local", label: "Local" },
        { id: "remote", label: "Remote (paid)" },
      ],
      models: [{ id: "demo-chat", label: "Demo chat" }],
      protocols: [{ id: "openai", label: "OpenAI" }],
      workflows: [],
      trust_tiers: [],
      resource_tiers: [],
      tags: [],
      status_visibility: [],
      target_visibility: [],
      target_activation: [],
    },
  },
};
const surfaces = ["people", "invites", "plans", "sign-in", "invite"] as const;
type Surface = (typeof surfaces)[number];
type State = "seeded" | "empty" | "error";

async function mock(page: Page, state: State) {
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json:
        state === "error"
          ? { error: { message: "People unavailable" } }
          : {
              people: state === "empty" ? [] : [person],
              profiles: state === "empty" ? [] : [plan],
            },
    }),
  );
  await page.route("**/admin/api/invites", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json:
        state === "error"
          ? { error: { message: "Invites unavailable" } }
          : {
              invites: state === "empty" ? [] : [invitation],
              profiles: state === "empty" ? [] : [plan],
            },
    }),
  );
  await page.route("**/admin/api/profiles", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json:
        state === "error"
          ? { error: { message: "Plans unavailable" } }
          : { profiles: state === "empty" ? [] : [plan] },
    }),
  );
  await page.route("**/auth/api/options*", (route) =>
    route.fulfill({
      status: state === "error" ? 503 : 200,
      json:
        state === "error"
          ? { error: { message: "Sign-in unavailable" } }
          : {
              ...signIn,
              ...(state === "empty" ? { built_in: false, oidc: false } : {}),
            },
    }),
  );
  await page.route("**/auth/api/invite?**", (route) =>
    route.fulfill({
      status: state === "error" ? 410 : 200,
      json:
        state === "error"
          ? {
              error: {
                message:
                  "This invitation has expired. Ask the owner for a new link.",
              },
            }
          : {
              ...signIn,
              plan,
              email: invitation.email,
              expires_at: invitation.expires_at,
            },
    }),
  );
  await page.route("**/admin/api/policy/editor", (route) =>
    route.fulfill({ json: editor }),
  );
}
async function open(page: Page, surface: Surface, state: State) {
  await mock(page, state);
  await page.goto(
    surface === "sign-in"
      ? "/ui/sign-in/"
      : surface === "invite"
        ? `/ui/invite/${state === "empty" ? "" : "#token=browser-test-token"}`
        : "/ui/people/",
  );
  if (surface === "invites")
    await page.getByRole("tab", { name: "Invites", exact: true }).click();
  if (surface === "plans")
    await page.getByRole("tab", { name: "Access plans", exact: true }).click();
  if (state === "error" && surface === "invite") {
    const alert = page
      .getByRole("alert")
      .filter({ hasText: /Couldn’t use this invitation/ });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(
      "The request could not be completed. Try again; if this continues, ask the gateway owner for help.",
    );
  } else if (state === "error")
    await expect(
      page
        .getByRole("alert")
        .filter({ hasText: /Couldn’t load (this|sign-in options)/ }),
    ).toBeVisible();
  else if (surface === "people")
    await expect(
      page.getByText(
        state === "empty" ? "Your people will appear here" : "Morgan Rivera",
        { exact: true },
      ),
    ).toBeVisible();
  else if (surface === "invites")
    await expect(
      page.getByText(
        state === "empty" ? "No invitations yet" : "sam@example.test",
        { exact: true },
      ),
    ).toBeVisible();
  else if (surface === "plans")
    await expect(
      state === "empty"
        ? page.getByText("Create your first access plan", { exact: true })
        : page.getByRole("heading", { name: plan.name, exact: true }),
    ).toBeVisible();
  else if (surface === "sign-in")
    await expect(
      state === "empty"
        ? page.getByText("Sign-in is not configured", { exact: true })
        : page.getByLabel("Password", { exact: true }),
    ).toBeVisible();
  else
    await expect(
      page.getByText(
        state === "empty"
          ? "You need an invite link"
          : `${signIn.owner_name} invited you to ${signIn.instance_name}.`,
        { exact: true },
      ),
    ).toBeVisible();
}
for (const theme of ["light", "dark"] as const)
  for (const width of [1440, 390])
    for (const surface of surfaces)
      for (const state of ["seeded", "empty", "error"] as const) {
        test(`people lane ${surface} ${state} ${theme} ${width}`, async ({
          page,
        }) => {
          await page.setViewportSize({
            width,
            height: width === 390 ? 844 : 1000,
          });
          await page.emulateMedia({
            colorScheme: theme,
            reducedMotion: "reduce",
          });
          await page.addInitScript(
            (value) => localStorage.setItem("theme", value),
            theme,
          );
          await open(page, surface, state);
          await page.evaluate(() => document.fonts.ready);
          expect(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
          ).toBe(true);
          const a11y = await new AxeBuilder({ page }).analyze();
          expect(
            a11y.violations
              .filter((v) => ["serious", "critical"].includes(v.impact || ""))
              .map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target) })),
          ).toEqual([]);
          await page.screenshot({
            path: `test-results/screens/people-${surface}-${state}-${theme}-${width}.png`,
            fullPage: true,
          });
        });
      }

test("people lane invite create, plan change, disable, reset and remove journeys", async ({
  page,
}) => {
  await open(page, "people", "seeded");
  await page.route("**/admin/api/invites", async (route) => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({
        profile_id: plan.id,
        email: "friend@example.test",
        expires_in: 604800,
      });
      await route.fulfill({
        json: {
          invite: invitation,
          url: "/ui/invite/#token=browser-test-token",
        },
      });
    } else
      await route.fulfill({
        json: { invites: [invitation], profiles: [plan] },
      });
  });
  await page
    .getByRole("button", { name: "Invite someone", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Access plan", exact: true })
    .click();
  await page.getByRole("option", { name: plan.name }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Chat and images. Local models only. No paid providers.",
  );
  await page.getByLabel("Email (optional)").fill("friend@example.test");
  await page.getByRole("button", { name: "Create invite link" }).click();
  await expect(page.getByRole("dialog")).toContainText("Invitation ready");
  await expect(page.getByLabel("Link", { exact: true })).toHaveValue(
    /#token=browser-test-token$/,
  );
  await page.getByRole("button", { name: "I’ve saved the link" }).click();
  await page.route(`**/admin/api/invites/${invitation.id}/link`, (route) =>
    route.fulfill({ json: { url: "/ui/invite/#token=browser-test-token" } }),
  );
  await page.reload();
  await page.getByRole("tab", { name: "Invites", exact: true }).click();
  await page
    .getByRole("button", { name: "Actions for invitation to sam@example.test" })
    .click();
  await page
    .getByRole("menuitem", { name: "Copy invite link", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Invitation link");
  await expect(page.getByLabel("Link", { exact: true })).toHaveValue(
    /#token=browser-test-token$/,
  );
  await page.getByRole("button", { name: "I’ve saved the link" }).click();
  await page.getByRole("tab", { name: "People", exact: true }).click();
  const writes: object[] = [];
  await page.route(`**/admin/api/people/${person.id}`, async (route) => {
    writes.push({
      method: route.request().method(),
      body: route.request().postDataJSON(),
    });
    await route.fulfill({ json: { person } });
  });
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Change access plan" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Chat and images. Local models only. No paid providers.",
  );
  await page.getByRole("button", { name: "Save access plan" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[0]).toEqual({ method: "PATCH", body: { profile_id: plan.id } });
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Disable access" }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[1]).toEqual({ method: "PATCH", body: { disabled: true } });
  await page.route(`**/admin/api/people/${person.id}/reset`, (route) =>
    route.fulfill({
      json: {
        url: "/ui/sign-in/#reset=reset-test",
        expires_at: invitation.expires_at,
      },
    }),
  );
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page
    .getByRole("menuitem", { name: "Create password reset link" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Password reset link");
  await page.getByRole("button", { name: "I’ve saved the link" }).click();
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page
    .getByRole("menuitem", { name: "Remove person", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Confirm", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Type Morgan Rivera to confirm").fill("Morgan Rivera");
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[2]).toEqual({ method: "DELETE", body: null });
});

test("people lane editing preserves unknown fields, omitted sets, and simulator returns actual decision", async ({
  page,
}) => {
  await open(page, "plans", "seeded");
  await page.route(`**/admin/api/profiles/${plan.id}`, (route) => {
    const body = route.request().postDataJSON();
    expect(body.profile.custom_operator_field).toEqual({ preserve: true });
    expect(body.profile.models).toEqual(["demo-chat"]);
    expect(body.profile.protocols).toBeUndefined();
    return route.fulfill({ json: { profile: { ...plan, ...body } } });
  });
  await page.getByRole("button", { name: "Edit plan", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Family access");
  await page.getByRole("button", { name: "Save access plan" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.route("**/admin/api/policy/simulate", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      profile: plan.id,
      request: { model: "demo-chat", intent: null, protocol: null },
    });
    return route.fulfill({
      json: {
        profile_id: plan.id,
        decision: {
          decision: "denied",
          allowed: false,
          reasons: ["This model is not allowed by the selected plan."],
          sources: [],
          visibility_level: "none",
        },
      },
    });
  });
  await page.getByRole("button", { name: "Test access", exact: true }).click();
  await page.getByLabel("Model", { exact: true }).fill("demo-chat");
  await page.getByRole("button", { name: "Run test" }).click();
  await expect(page.getByRole("status")).toContainText(
    "This request would be denied",
  );
  await expect(page.getByRole("status")).toContainText(
    "This model is not allowed by the selected plan.",
  );
  await page.getByLabel("Model", { exact: true }).fill("different-model");
  await expect(page.getByText("This request would be denied")).toHaveCount(0);
});

test("people lane account acceptance reveals a key once, then lands on Home", async ({
  page,
}) => {
  await open(page, "invite", "seeded");
  await page.route("**/auth/api/invite/accept", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      token: "browser-test-token",
      name: "Sam Rivera",
      email: invitation.email,
      password: "Browser acceptance password",
    });
    return route.fulfill({
      json: { next: "/ui/", credential: { api_key: "test-key-display-once" } },
    });
  });
  await page.getByLabel("Your name").fill("Sam Rivera");
  await page
    .getByLabel("Password", { exact: true })
    .fill("Browser acceptance password");
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Your account is ready" }),
  ).toBeVisible();
  await expect(page.getByLabel("Your key", { exact: true })).toHaveValue(
    "test-key-display-once",
  );
  expect(new URL(page.url()).hash).toBe("");
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(page).toHaveURL(/\/ui\/$/);
});

test("people lane sign-in handles denial, retries successfully, and resets password", async ({
  page,
}) => {
  await open(page, "sign-in", "seeded");
  let failed = true;
  await page.route("**/auth/api/login", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      email: person.email,
      password: "Browser sign in password",
      return_to: "/ui/home/",
    });
    return route.fulfill({
      status: failed ? 401 : 200,
      json: failed
        ? { error: { message: "Email or password is incorrect." } }
        : { next: "/ui/" },
    });
  });
  await page.getByLabel("Email", { exact: true }).fill(person.email);
  await page
    .getByLabel("Password", { exact: true })
    .fill("Browser sign in password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Email or password is incorrect." }),
  ).toContainText("Email or password is incorrect.");
  failed = false;
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/$/);
  await page.goto("/ui/sign-in/#reset=reset-browser-token");
  await expect(
    page.getByRole("heading", { name: "Choose a new password" }),
  ).toBeVisible();
  await page.route("**/auth/api/password-reset", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      token: "reset-browser-token",
      password: "Browser replacement password",
    });
    return route.fulfill({ json: { next: "/ui/sign-in/" } });
  });
  await page
    .getByLabel("New password", { exact: true })
    .fill("Browser replacement password");
  await page.getByRole("button", { name: "Save password" }).click();
  await expect(page).toHaveURL(/\/ui\/sign-in\/$/);
  await expect(
    page.getByRole("heading", { name: "Sign in", exact: true }),
  ).toBeVisible();
});

test("people lane failed list retries, pending invitation revokes with confirmation", async ({
  page,
}) => {
  await open(page, "invites", "error");
  await page.unroute("**/admin/api/invites");
  await page.route("**/admin/api/invites", (route) =>
    route.fulfill({ json: { invites: [invitation], profiles: [plan] } }),
  );
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText(invitation.email, { exact: true })).toBeVisible();
  let revoked = false;
  await page.route(`**/admin/api/invites/${invitation.id}`, (route) => {
    revoked = true;
    expect(route.request().method()).toBe("DELETE");
    return route.fulfill({ json: { invite: invitation } });
  });
  await page
    .getByRole("button", { name: "Actions for invitation to sam@example.test" })
    .click();
  await page.getByRole("menuitem", { name: "Revoke invitation" }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(revoked).toBe(true);
});

test("people lane invitation rows distinguish pending, accepted, expired and revoked", async ({
  page,
}) => {
  const now = Math.floor(Date.now() / 1000);
  const invites = [
    invitation,
    {
      ...invitation,
      id: "invitation-accepted",
      email: "accepted@example.test",
      used_at: now - 120,
    },
    {
      ...invitation,
      id: "invitation-expired",
      email: "expired@example.test",
      expires_at: now - 120,
    },
    {
      ...invitation,
      id: "invitation-revoked",
      email: "revoked@example.test",
      revoked_at: now - 120,
    },
  ];
  await mock(page, "seeded");
  await page.route("**/admin/api/invites", (route) =>
    route.fulfill({ json: { invites, profiles: [plan] } }),
  );
  await page.goto("/ui/people/");
  await page.getByRole("tab", { name: "Invites", exact: true }).click();
  for (const status of ["Pending", "Accepted", "Expired", "Revoked"]) {
    await expect(page.getByText(status, { exact: true })).toBeVisible();
  }
  await page
    .getByRole("button", {
      name: "Actions for invitation to accepted@example.test",
    })
    .click();
  await expect(
    page.getByRole("menuitem", { name: "Copy invite link", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("menuitem", { name: "Revoke invitation", exact: true }),
  ).toBeDisabled();
});

test("people lane OIDC invitation resumes its private token and accepts with session CSRF", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.addInitScript(() =>
    sessionStorage.setItem("gateway-invitation", "oidc-resume-token"),
  );
  await page.route("**/auth/api/options*", (route) =>
    route.fulfill({
      json: {
        ...signIn,
        built_in: false,
        signed_in_oidc: {
          email: invitation.email,
          name: "Sam Rivera",
          csrf_token: "test-oidc-csrf",
        },
      },
    }),
  );
  await page.route("**/auth/api/invite/accept", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      token: "oidc-resume-token",
      name: "Sam Rivera",
      email: invitation.email,
    });
    expect(route.request().headers()["x-ai-gateway-csrf"]).toBe(
      "test-oidc-csrf",
    );
    return route.fulfill({
      json: { next: "/ui/", credential: { api_key: "oidc-test-display-once" } },
    });
  });
  await page.goto("/ui/invite/");
  await expect(
    page.getByText("Accept as sam@example.test.", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Accept invitation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Your account is ready" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => sessionStorage.getItem("gateway-invitation")),
  ).toBeNull();
});

test("people lane real invitation creates an account, app and working key, then revokes access", async ({
  browser,
  baseURL,
  request,
}) => {
  const memberContext = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const memberPage = await memberContext.newPage();
  try {
    const session = await (await request.get("/admin/api/session")).json();
    const headers = { "x-ai-gateway-csrf": session.actor.csrf_token };
    const profiles = await (await request.get("/admin/api/profiles")).json();
    const local = profiles.profiles.find(
      (p: { name: string }) => p.name === "Local chat",
    );
    expect(local).toBeTruthy();
    const result = await request.post("/admin/api/invites", {
      headers,
      data: {
        profile_id: local.id,
        email: "real-browser-member@example.test",
        expires_in: 86400,
      },
    });
    expect(result.ok()).toBe(true);
    const created = await result.json();
    const recovered = await request.get(
      `/admin/api/invites/${created.invite.id}/link`,
    );
    expect(recovered.ok()).toBe(true);
    expect((await recovered.json()).url).toBe(created.url);
    await memberPage.goto(created.url);
    await memberPage.getByLabel("Your name").fill("Real browser member");
    await memberPage
      .getByLabel("Password", { exact: true })
      .fill("Real browser acceptance password");
    const acceptedResponse = memberPage.waitForResponse((r) =>
      r.url().endsWith("/auth/api/invite/accept"),
    );
    await memberPage
      .getByRole("button", { name: "Create account", exact: true })
      .click();
    const accepted = await (await acceptedResponse).json();
    await expect(
      memberPage.getByRole("heading", { name: "Your account is ready" }),
    ).toBeVisible();
    expect(
      (
        await request.get("/openai/v1/models", {
          headers: { authorization: `Bearer ${accepted.credential.api_key}` },
        })
      ).ok(),
    ).toBe(true);
    await memberPage.getByRole("button", { name: "I’ve saved it" }).click();
    await expect(memberPage).toHaveURL(/\/ui\/$/);
    const member = await memberContext.request.get("/me/api/session");
    const memberData = await member.json();
    expect(memberData.authenticated).toBe(true);
    expect(memberData.user.email).toBe("real-browser-member@example.test");
    expect(
      memberData.apps.some(
        (app: { name: string }) => app.name === "My first app",
      ),
    ).toBe(true);
    const ownerPeople = await (await request.get("/admin/api/people")).json();
    const added = ownerPeople.people.find(
      (p: { email: string }) => p.email === "real-browser-member@example.test",
    );
    expect(added).toBeTruthy();
    const removed = await request.delete(`/admin/api/people/${added.id}`, {
      headers,
    });
    expect(removed.ok()).toBe(true);
    expect(
      (
        await request.get("/openai/v1/models", {
          headers: { authorization: `Bearer ${accepted.credential.api_key}` },
        })
      ).status(),
    ).toBe(401);
    let revokedMemberSession =
      await memberContext.request.get("/me/api/session");
    if (revokedMemberSession.status() === 401) {
      revokedMemberSession = await memberContext.request.get("/me/api/session");
    }
    expect(revokedMemberSession.status()).toBe(200);
    const revokedMemberData = await revokedMemberSession.json();
    expect(revokedMemberData.authenticated).toBe(false);
    expect(revokedMemberData.user).toBeUndefined();
    expect(revokedMemberData.apps).toEqual([]);
  } finally {
    await memberContext.close();
  }
});

test("people lane protects owner actions and explains permanent key revocation", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({
      json: {
        people: [{ ...person, builtin: false, protected: true }],
        profiles: [plan],
      },
    }),
  );
  await page.goto("/ui/people/");
  await expect(
    page.getByText("Protected account", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Actions for Morgan Rivera" }),
  ).toHaveCount(0);
  await page.unroute("**/admin/api/people");
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({
      json: {
        people: [{ ...person, builtin: true, protected: true, role: "owner" }],
        profiles: [plan],
      },
    }),
  );
  await page.reload();
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await expect(
    page.getByRole("menuitem", { name: "Create password reset link" }),
  ).toBeVisible();
  await expect(
    page.getByRole("menuitem", { name: "Change access plan" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("menuitem", { name: "Disable access" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("menuitem", { name: "Remove person" }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.unroute("**/admin/api/people");
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({
      json: {
        people: [{ ...person, builtin: false, protected: false }],
        profiles: [plan],
      },
    }),
  );
  await page.reload();
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await expect(
    page.getByRole("menuitem", { name: "Create password reset link" }),
  ).toHaveCount(0);
  await page.getByRole("menuitem", { name: "Disable access" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "keys are permanently revoked",
  );
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.unroute("**/admin/api/people");
  await page.route("**/admin/api/people", (route) =>
    route.fulfill({
      json: {
        people: [
          { ...person, builtin: true, protected: false, disabled: true },
        ],
        profiles: [plan],
      },
    }),
  );
  await page.reload();
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Enable access" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Previously revoked keys stay revoked",
  );
});
