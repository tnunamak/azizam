import { type Page } from "@playwright/test";
import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import AxeBuilder from "@axe-core/playwright";
import { fulfilJson } from "./fixtures/intercept";

// People cases share the lane owner's invitations and people list, so they run
// in order on one worker (in parallel one case re-rendered another's list and
// focus did not return to the invitation's Actions trigger).
test.describe.configure({ mode: "default" });

// The shapes the gateway sends (docs/design/ACCESS_PLANS.md §3.1).
const planDocument = {
  name: "Local creative",
  can_use: { chat: { items: "all" }, image: { items: "all" } },
  direct_engine_access: { engines: [], operations: [] },
  paid_providers: [],
  provider_fallback: false,
  caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
  can_load_models: true,
  receipt_detail: "basic",
  media_metadata: "none",
};
const planSummary = "Chat and images. Local models only. No paid providers.";
const plan = {
  id: "plan-local",
  name: "Local creative",
  created_at: 1720000000,
  updated_at: 1720000000,
  plan: { ...planDocument, id: "plan-local" },
  summary: planSummary,
  used_by: { people: ["person-family"], apps: [], invites: 1 },
};
// People and Invites list plans as pickers show them.
const planChoice = { id: plan.id, name: plan.name, summary: planSummary };
const person = {
  id: "person-family",
  name: "Morgan Rivera",
  email: "morgan@example.test",
  plan_id: plan.id,
  plan_name: plan.name,
  disabled_at: null,
  last_active: Math.floor(Date.now() / 1000) - 3600,
  disabled: false,
  requests_this_month: null,
  builtin: true,
  protected: false,
  role: "member",
  grants: [],
};
const invitation = {
  id: "invitation-family",
  kind: "invite",
  email: "sam@example.test",
  plan_id: plan.id,
  plan_name: plan.name,
  user_id: null,
  created_by: "owner",
  expires_at: Math.floor(Date.now() / 1000) + 86400,
  created_at: Math.floor(Date.now() / 1000) - 3600,
  used_at: null,
  revoked_at: null,
};
const signIn = {
  built_in: true,
  oidc: true,
  oidc_label: "Example ID",
  oidc_url: "/login?return_to=/",
  instance_name: "Rivera household",
  owner_name: "Alex",
};
const editor = {
  schema_version: 2,
  capabilities: [
    { id: "chat", label: "Chat", items: [{ kind: "model", id: "demo-chat", available: true }] },
    { id: "image", label: "Images", items: [] },
  ],
  engines: [],
  paid_providers: [],
  receipt_detail: [{ id: "basic", label: "Basic", description: null }],
  media_metadata: [{ id: "none", label: "Strip", description: null }],
  new_plan: { ...planDocument, name: "", can_use: {} },
};
const surfaces = ["people", "invites", "plans", "sign-in", "invite"] as const;
type Surface = (typeof surfaces)[number];
type State = "seeded" | "empty" | "error";

async function mock(page: Page, state: State) {
  await page.route("**/admin/api/people", (route) =>
    fulfilJson(route, state === "error"
          ? { error: { message: "People unavailable" } }
          : {
              people: state === "empty" ? [] : [person],
              access_plans: state === "empty" ? [] : [planChoice],
            }, { status: state === "error" ? 503 : 200 }),
  );
  await page.route("**/admin/api/invites", (route) =>
    fulfilJson(route, state === "error"
          ? { error: { message: "Invites unavailable" } }
          : {
              invites: state === "empty" ? [] : [invitation],
              access_plans: state === "empty" ? [] : [planChoice],
              invite_access_plan: null,
            }, { status: state === "error" ? 503 : 200 }),
  );
  await page.route("**/admin/api/access-plans", (route) =>
    fulfilJson(route, state === "error"
          ? { error: { message: "Plans unavailable" } }
          : { access_plans: state === "empty" ? [] : [plan] }, { status: state === "error" ? 503 : 200 }),
  );
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, state === "error"
          ? { error: { message: "Sign-in unavailable" } }
          : {
              ...signIn,
              ...(state === "empty" ? { built_in: false, oidc: false } : {}),
            }, { status: state === "error" ? 503 : 200 }),
  );
  await page.route("**/auth/api/invite", (route) =>
    fulfilJson(route, state === "error"
          ? {
              error: {
                message:
                  "This invitation has expired. Ask the owner for a new link.",
              },
            }
          : {
              ...signIn,
              plan: planChoice,
              email: invitation.email,
              expires_at: invitation.expires_at,
            }, { status: state === "error" ? 410 : 200 }),
  );
  await page.route("**/admin/api/access-plans/editor", (route) =>
    fulfilJson(route, editor),
  );
}
async function open(page: Page, surface: Surface, state: State) {
  await mock(page, state);
  await page.goto(
    surface === "sign-in"
      ? "/sign-in/"
      : surface === "invite"
        ? `/invite/${state === "empty" ? "" : "#token=browser-test-token"}`
        : "/people/",
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
        .filter({ hasText: /Couldn’t load (this|sign-in)|Something didn’t load/ }),
    ).toBeVisible();
  else if (surface === "people")
    await expect(
      page.getByText(
        state === "empty"
          ? "Create an access plan before inviting someone"
          : "Morgan Rivera",
        { exact: true },
      ),
    ).toBeVisible();
  else if (surface === "invites")
    await expect(
      page.getByText(
        state === "empty"
          ? "Create an access plan before inviting someone"
          : "sam@example.test",
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
          if (surface === "people" && state === "seeded") {
            const inviteAction = page.getByRole("button", {
              name: "Invite someone",
              exact: true,
            });
            await expect(inviteAction).toBeEnabled();
            await expect(inviteAction).toHaveCSS("opacity", "1");
          }
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
        access_plan: plan.id,
        email: "friend@example.test",
        expires_in: 604800,
      });
      await fulfilJson(route, {
          invite: invitation,
          url: "/invite/#token=browser-test-token",
        });
    } else
      await fulfilJson(route, { invites: [invitation], access_plans: [planChoice], invite_access_plan: null });
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
    fulfilJson(route, { url: "/invite/#token=browser-test-token" }),
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
    await fulfilJson(route, { ok: true });
  });
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Change access plan" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Chat and images. Local models only. No paid providers.",
  );
  await page.getByRole("button", { name: "Save access plan" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[0]).toEqual({ method: "PATCH", body: { access_plan: plan.id } });
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Disable access" }).click();
  await page
    .getByRole("button", { name: "Disable access", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[1]).toEqual({ method: "PATCH", body: { disabled: true } });
  await page.route(`**/admin/api/people/${person.id}/reset`, (route) =>
    fulfilJson(route, {
        url: "/sign-in/#reset=reset-test",
        expires_at: invitation.expires_at,
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
    page.getByRole("button", { name: "Remove person", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Type Morgan Rivera to confirm").fill("Morgan Rivera");
  await page
    .getByRole("button", { name: "Remove person", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes[2]).toEqual({ method: "DELETE", body: null });
});

test("people lane editing sends the whole plan document, and Check access lists what the plan can and cannot use", async ({
  page,
}) => {
  await open(page, "plans", "seeded");
  await page.route(`**/admin/api/access-plans/${plan.id}`, (route) => {
    const body = route.request().postDataJSON();
    expect(route.request().method()).toBe("PUT");
    // Every field goes back, so the server never fills a gap with a default.
    expect(body).toEqual({ ...planDocument, name: "Family access" });
    return fulfilJson(route, { access_plan: { ...plan, name: body.name, plan: { ...body, id: plan.id } } });
  });
  await page.getByRole("button", { name: "Edit plan", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Family access");
  await page.getByRole("button", { name: "Save access plan" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.route("**/admin/api/access/preview", (route) => {
    expect(route.request().postDataJSON()).toEqual({ plan: plan.id });
    return fulfilJson(route, {
      plan_ids: [plan.id],
      summary: { usable: 1, total: 2 },
      entries: [
        {
          id: "demo-chat",
          name: "Demo chat",
          alias: false,
          group: "chat",
          group_label: "Chat",
          state: "allowed",
          reason: null,
          kinds: [],
        },
        {
          id: "demo-video",
          name: "Demo video",
          alias: false,
          group: "video",
          group_label: "Video",
          state: "blocked",
          reason: "demo-video isn't in your access plan.",
          kinds: [],
        },
      ],
    });
  });
  await page.getByRole("button", { name: "Check access", exact: true }).click();
  const dialog = page.getByRole("dialog");
  // The answer is there without typing anything.
  await expect(dialog.getByText("Can use 1 of 2")).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  const blocked = dialog.getByRole("listitem").filter({ hasText: "Demo video" });
  await expect(blocked).toContainText("Blocked");
  await expect(blocked).toContainText("demo-video isn't in your access plan.");
  await expect(dialog.getByRole("listitem").filter({ hasText: "Demo chat" })).toContainText("Allowed");
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
    return fulfilJson(route, { next: "/", credential: { api_key: "test-key-display-once" } });
  });
  await page.getByLabel("Your name").fill("Sam Rivera");
  await page
    .getByLabel("Password", { exact: true })
    .fill("Browser acceptance password");
  await page.getByLabel("Confirm password").fill("Different password");
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Passwords do not match" }),
  ).toBeVisible();
  await page.getByLabel("Confirm password").fill("Browser acceptance password");
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Your account is ready" }),
  ).toBeVisible();
  await expect(page.getByLabel("Your key", { exact: true })).toHaveValue(
    "test-key-display-once",
  );
  await expect(page.getByText(/API key is for apps/)).toBeVisible();
  await expect(
    page.getByText(/sign in with your email and password/),
  ).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  await page.getByRole("button", { name: "I’ve saved it" }).click();
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
});

test("people lane unbound invitation requires an explicit choice", async ({
  page,
}) => {
  await open(page, "people", "seeded");
  await page
    .getByRole("button", { name: "Invite someone", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Access plan", exact: true })
    .click();
  await page.getByRole("option", { name: plan.name }).click();
  await expect(
    page.getByRole("button", { name: "Create invite link" }),
  ).toBeDisabled();
  await page.getByLabel("Allow anyone with the link").check();
  await expect(
    page.getByRole("button", { name: "Create invite link" }),
  ).toBeEnabled();
});

test("people lane sign-in handles denial, retries successfully, and resets password", async ({
  page,
}) => {
  await open(page, "sign-in", "seeded");
  await expect(
    page.getByText(/Forgot your password\? Ask the person who invited you/),
  ).toBeVisible();
  let failed = true;
  await page.route("**/auth/api/login", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      email: person.email,
      password: "Browser sign in password",
      return_to: "/",
    });
    return fulfilJson(route, failed
        ? {
            error: {
              code: "invalid_login",
              message: "Email or password is incorrect.",
            },
          }
        : { next: "/" }, { status: failed ? 401 : 200 });
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
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
  await page.goto("/sign-in/#reset=reset-browser-token");
  await expect(
    page.getByRole("heading", { name: "Choose a new password" }),
  ).toBeVisible();
  await page.route("**/auth/api/password-reset", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      token: "reset-browser-token",
      password: "Browser replacement password",
    });
    return fulfilJson(route, { next: "/sign-in/" });
  });
  await page
    .getByLabel("New password", { exact: true })
    .fill("Browser replacement password");
  await page.getByRole("button", { name: "Save password" }).click();
  await expect(page).toHaveURL(/\/sign-in\/$/);
  await expect(
    page.getByRole("heading", {
      name: `Sign in to ${signIn.instance_name}`,
      exact: true,
    }),
  ).toBeVisible();
});

test("people lane failed list retries, pending invitation revokes with confirmation", async ({
  page,
}) => {
  await open(page, "invites", "error");
  await page.unroute("**/admin/api/invites");
  await page.route("**/admin/api/invites", (route) =>
    fulfilJson(route, { invites: [invitation], access_plans: [planChoice] }),
  );
  // The plans request also fails in the error state, so the header explains
  // why Invite is unavailable and offers its own Try again.
  await expect(
    page.getByText("Access plans couldn’t be loaded", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Invites")
    .getByRole("button", { name: "Try again", exact: true })
    .click();
  await expect(page.getByText(invitation.email, { exact: true })).toBeVisible();
  let revoked = false;
  await page.route(`**/admin/api/invites/${invitation.id}`, (route) => {
    revoked = true;
    expect(route.request().method()).toBe("DELETE");
    return fulfilJson(route, { invite: invitation });
  });
  await page
    .getByRole("button", { name: "Actions for invitation to sam@example.test" })
    .click();
  await page.getByRole("menuitem", { name: "Revoke invitation" }).click();
  await page
    .getByRole("button", { name: "Revoke invitation", exact: true })
    .click();
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
    fulfilJson(route, { invites, access_plans: [planChoice] }),
  );
  await page.goto("/people/");
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

test("people lane omits unknown monthly usage and keeps known usage", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.route("**/admin/api/people", (route) =>
    fulfilJson(route, {
        people: [
          person,
          {
            ...person,
            id: "person-active",
            name: "Casey Rivera",
            email: "casey@example.test",
            requests_this_month: 42,
          },
        ],
        access_plans: [planChoice],
      }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/people/");
  const unknownRow = page
    .getByRole("listitem")
    .filter({ hasText: "Morgan Rivera" });
  const knownRow = page
    .getByRole("listitem")
    .filter({ hasText: "Casey Rivera" });
  await expect(unknownRow).toBeVisible();
  await expect(unknownRow).not.toContainText("This month");
  await expect(knownRow).toContainText("This month · 42 requests");
});

test("people lane OIDC invitation resumes its private token and accepts with session CSRF", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.addInitScript(() =>
    sessionStorage.setItem("gateway-invitation", "oidc-resume-token"),
  );
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, {
        ...signIn,
        built_in: false,
        signed_in_oidc: {
          email: invitation.email,
          name: "Sam Rivera",
          csrf_token: "test-oidc-csrf",
        },
      }),
  );
  await page.route("**/auth/api/invite/accept", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      token: "oidc-resume-token",
      name: "Sam Rivera",
      email: invitation.email,
    });
    expect(route.request().headers()["x-azizam-csrf"]).toBe("test-oidc-csrf");
    return fulfilJson(route, { next: "/", credential: { api_key: "oidc-test-display-once" } });
  });
  await page.goto("/invite/");
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

test("people lane real invitation creates an account, app and working key, then revokes access", { tag: GLOBAL_STATE_TAG }, async ({
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
    const headers = { "x-azizam-csrf": session.actor.csrf_token };
    const plans = await (await request.get("/admin/api/access-plans")).json();
    const local = plans.access_plans.find(
      (p: { name: string }) => p.name === "Local chat",
    );
    expect(local).toBeTruthy();
    const result = await request.post("/admin/api/invites", {
      headers,
      data: {
        access_plan: local.id,
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
    await memberPage
      .getByLabel("Confirm password")
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
    await expect(memberPage).toHaveURL(/^https?:\/\/[^/]+\/$/);
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
    fulfilJson(route, {
        people: [{ ...person, builtin: false, protected: true }],
        access_plans: [planChoice],
      }),
  );
  await page.goto("/people/");
  await expect(
    page.getByText("Protected account", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Actions for Morgan Rivera" }),
  ).toHaveCount(0);
  await page.unroute("**/admin/api/people");
  await page.route("**/admin/api/people", (route) =>
    fulfilJson(route, {
        people: [{ ...person, builtin: true, protected: true, role: "owner" }],
        access_plans: [planChoice],
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
    fulfilJson(route, {
        people: [{ ...person, builtin: false, protected: false }],
        access_plans: [planChoice],
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
    fulfilJson(route, {
        people: [
          { ...person, builtin: true, protected: false, disabled: true },
        ],
        access_plans: [planChoice],
      }),
  );
  await page.reload();
  await page.getByRole("button", { name: "Actions for Morgan Rivera" }).click();
  await page.getByRole("menuitem", { name: "Enable access" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Previously revoked keys stay revoked",
  );
});

// People/Settings red-team regression coverage; all API state is test-owned.
test("administrators cannot invite or manage people but can view access plans", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.route("**/me/api/session", (route) =>
    fulfilJson(route, {
        authenticated: true,
        user: {
          id: "admin",
          name: "Admin",
          email: "admin@example.test",
          role: "admin",
        },
      }),
  );
  await page.goto("/people/");
  await expect(
    page.getByRole("button", { name: "Invite someone", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Actions for Morgan Rivera" }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "Invites", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /Actions for invitation/ }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "Access plans", exact: true }).click();
  await expect(
    page.getByText("Only the owner can change access plans.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit plan" })).toHaveCount(0);
});

test("an invalid plan document stays editable with an inline error", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.goto("/people/?tab=plans");
  await page.getByRole("button", { name: "Edit plan" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText("Details", { exact: true }).click();
  await dialog.getByText("Complete plan document", { exact: true }).click();
  await dialog.getByLabel("Plan JSON").fill('{"name":"No fields"}');
  await expect(dialog.getByRole("alert")).toHaveText(
    "The plan needs the field can_use.",
  );
  await expect(dialog.getByLabel("Plan JSON")).toHaveValue('{"name":"No fields"}');
  await expect(
    dialog.getByRole("button", { name: "Save access plan" }),
  ).toBeDisabled();
  await dialog.getByLabel("Plan JSON").fill(JSON.stringify(planDocument));
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Save access plan" }),
  ).toBeEnabled();
});

for (const disabled of [false, true]) {
  test(`${disabled ? "enable" : "disable"} confirmation names its person and restores keyboard focus`, async ({
    page,
  }) => {
    await mock(page, "seeded");
    await page.route("**/admin/api/people", (route) =>
      fulfilJson(route, { people: [{ ...person, disabled }], access_plans: [planChoice] }),
    );
    await page.goto("/people/");
    const trigger = page.getByRole("button", {
      name: "Actions for Morgan Rivera",
    });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const action = disabled ? "Enable access" : "Disable access";
    await page.getByRole("menuitem", { name: action }).click();
    await expect(page.getByRole("dialog")).toContainText(
      "Morgan Rivera (morgan@example.test)",
    );
    await expect(
      page.getByRole("dialog").getByRole("button", { name: action }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
  });
}

test("invitation revoke names the recipient and link dialogs use the public URL", async ({
  page,
}) => {
  await mock(page, "seeded");
  await page.route("**/admin/api/settings/instance", (route) =>
    fulfilJson(route, { instance: { base_url: "https://public.example.test" } }),
  );
  await page.route("**/admin/api/invites/invitation-family/link", (route) =>
    fulfilJson(route, { url: "/invite/#token=fixture" }),
  );
  await page.goto("/people/?tab=invites");
  const trigger = page.getByRole("button", {
    name: "Actions for invitation to sam@example.test",
  });
  await trigger.click();
  await page.getByRole("menuitem", { name: "Revoke invitation" }).click();
  await expect(page.getByRole("dialog")).toContainText("sam@example.test");
  await expect(
    page.getByRole("dialog").getByRole("button", { name: "Revoke invitation" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole("menuitem", { name: "Copy invite link" }).click();
  await expect(
    page.getByRole("textbox", { name: "Link", exact: true }),
  ).toHaveValue("https://public.example.test/invite/#token=fixture");
  await page.getByRole("button", { name: "I’ve saved the link" }).click();
  await expect(trigger).toBeFocused();
});

// Craft PR 3: a person row is two lines. A long name or plan stays inside
// its row: it fits, or ends in an ellipsis and keeps its full text in a title.
for (const width of [390, 1440]) {
  test(`long person and plan names stay within their rows at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mock(page, "seeded");
    await page.route("**/admin/api/people", (route) =>
      fulfilJson(route, {
          people: [
            { ...person, name: "n".repeat(300), plan_name: "p".repeat(300) },
          ],
          access_plans: [planChoice],
        }),
    );
    await page.goto("/people/");
    for (const text of ["n".repeat(300), "p".repeat(300)]) {
      // The plan is a column on wider screens and joins the second line on
      // a phone; one copy is visible.
      const name = page
        .getByText(text, { exact: true })
        .filter({ visible: true });
      await expect(name).toBeVisible();
      expect(
        await name.evaluate((el, full) => {
          const clip = (el.closest(".truncate") ?? el) as HTMLElement;
          const fits = clip.scrollWidth <= clip.clientWidth;
          const kept =
            getComputedStyle(clip).textOverflow === "ellipsis" &&
            (el.closest("[title]")?.getAttribute("title") ?? "").includes(full);
          return (
            clip.getBoundingClientRect().right <= innerWidth && (fits || kept)
          );
        }, text),
      ).toBe(true);
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("relative links wait for public settings while absolute server links are immediately usable", async ({
  page,
}) => {
  await mock(page, "seeded");
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/admin/api/settings/instance", async (route) => {
    await pending;
    await fulfilJson(route, { instance: { base_url: "https://public.example.test" } });
  });
  await page.route("**/admin/api/invites/invitation-family/link", (route) =>
    fulfilJson(route, { url: "/invite/#token=fixture" }),
  );
  await page.goto("/people/?tab=invites");
  await page
    .getByRole("button", { name: "Actions for invitation to sam@example.test" })
    .click();
  await page.getByRole("menuitem", { name: "Copy invite link" }).click();
  await expect(
    page.getByRole("textbox", { name: "Link", exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Copy link", exact: true }),
  ).toBeDisabled();
  release();
  await expect(
    page.getByRole("textbox", { name: "Link", exact: true }),
  ).toHaveValue("https://public.example.test/invite/#token=fixture");
  await expect(
    page.getByRole("button", { name: "Copy link", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "I’ve saved the link" }).click();
  await page.route("**/admin/api/invites/invitation-family/link", (route) =>
    fulfilJson(route, { url: "https://server.example.test/invite/#token=absolute" }),
  );
  await page
    .getByRole("button", { name: "Actions for invitation to sam@example.test" })
    .click();
  await page.getByRole("menuitem", { name: "Copy invite link" }).click();
  await expect(
    page.getByRole("textbox", { name: "Link", exact: true }),
  ).toHaveValue("https://server.example.test/invite/#token=absolute");
});

for (const tab of ["People", "Invites"] as const) {
  test(`${tab} owner denial shows an explanation without a retry`, async ({
    page,
  }) => {
    await mock(page, "seeded");
    await page.route(
      `**/admin/api/${tab === "People" ? "people" : "invites"}`,
      (route) =>
        fulfilJson(route, {
            error: { code: "owner_required", message: "Owner required" },
          }, { status: 403 }),
    );
    await page.goto("/people/");
    if (tab === "Invites")
      await page.getByRole("tab", { name: tab, exact: true }).click();
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      "Only the owner can change this.",
    );
    await expect(
      page.getByRole("button", { name: "Try again", exact: true }),
    ).toHaveCount(0);
  });
}
