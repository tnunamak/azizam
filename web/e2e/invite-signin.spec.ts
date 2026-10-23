// Principal state: the invite creates a member, so the address is namespaced
// and two copies of this case can run at once (fixtures/isolation.ts).
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

test("invited member creates a password account and signs in again", async ({
  browser,
  page,
  baseURL,
  isolated,
}) => {
  const email = isolated.email("invited");
  const session = await (await page.request.get("/admin/api/session")).json();
  const plans = await (await page.request.get("/admin/api/access-plans")).json();
  const plan = plans.access_plans.find(
    (item: { name: string }) => item.name === "Local chat",
  );
  expect(plan).toBeDefined();
  const inviteResponse = await page.request.post("/admin/api/invites", {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
    data: { access_plan: plan.id, email },
  });
  expect(inviteResponse.ok()).toBe(true);
  const invite = await inviteResponse.json();

  const guest = await browser.newContext({
    baseURL,
    // Sign-in ends the incoming session family; never inherit the lane owner.
    storageState: { cookies: [], origins: [] },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  try {
    const firstVisit = await guest.newPage();
    await firstVisit.goto(invite.url);
    await firstVisit.getByLabel("Your name").fill("Invited Member");
    await firstVisit
      .getByLabel("Password", { exact: true })
      .fill("A long browser test password");
    await firstVisit
      .getByLabel("Confirm password")
      .fill("A long browser test password");
    await firstVisit.getByRole("button", { name: "Create account" }).click();
    await expect(
      firstVisit.getByRole("heading", { name: "Your account is ready" }),
    ).toBeVisible();
    expect(
      (await page.request.get("/admin/api/session")).status(),
      "accepting an invite preserves the lane owner's session",
    ).toBe(200);
    await expect(
      firstVisit.getByText(/sign in with your email and password/),
    ).toBeVisible();
    const key = await firstVisit
      .getByLabel("Your key", { exact: true })
      .inputValue();
    await firstVisit.getByRole("button", { name: "Copy your key" }).click();
    const copied = await firstVisit.evaluate(() =>
      navigator.clipboard.readText(),
    );
    expect(copied === key).toBe(true);
    await firstVisit.getByRole("button", { name: "I’ve saved it" }).click();
    await expect(firstVisit).toHaveURL(/^https?:\/\/[^/]+\/$/);

    // The new member reads their own scoped Status without touching owner APIs.
    const ownerCalls: string[] = [];
    firstVisit.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/admin/api/")) {
        ownerCalls.push(request.url());
      }
    });
    const status = await (await guest.request.get("/me/api/status")).json();
    expect(status.scope).toBe("member");
    const chat = status.capabilities.find(
      (capability: { capability: string }) => capability.capability === "chat",
    );
    expect(chat).toMatchObject({
      use_names: expect.arrayContaining(["demo-chat"]),
      recommended_model: expect.any(String),
    });
    expect(chat.recommended_model.trim()).not.toBe("");
    await firstVisit.goto("/status/");
    await expect(firstVisit).toHaveURL(/\/status\/$/);
    await firstVisit.getByText("Model names", { exact: true }).click();
    await expect(
      firstVisit.getByText(`Chat · ${chat.recommended_model}`, { exact: true }),
    ).toBeVisible();
    expect(ownerCalls).toEqual([]);

    const returnVisit = await browser.newContext({
      baseURL,
      storageState: { cookies: [], origins: [] },
    });
    try {
      const signIn = await returnVisit.newPage();
      await signIn.goto("/sign-in/");
      await signIn.getByLabel("Email", { exact: true }).fill(email);
      await signIn
        .getByLabel("Password", { exact: true })
        .fill("A long browser test password");
      await signIn
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
      await expect(signIn).toHaveURL(/^https?:\/\/[^/]+\/$/);
      expect(
        (await page.request.get("/admin/api/session")).status(),
        "signing in again preserves the lane owner's session",
      ).toBe(200);
    } finally {
      await returnVisit.close();
    }
  } finally {
    await guest.close();
  }
});

test("a refused invitation sign-in explains itself, and the retry goes back to the invitation", async ({
  page,
}) => {
  await page.route("**/auth/api/options*", (route) =>
    fulfilJson(route, {
      built_in: false,
      oidc: true,
      oidc_label: "Household accounts",
      oidc_url: "/login?return_to=%2Finvite%2F",
      instance_name: "Azizam",
    }),
  );
  await page.goto("/sign-in/?error=not_fresh&return_to=%2Finvite%2F&provider_sign_out=1");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Azizam couldn’t confirm that Household accounts just asked for your password.",
  );
  await expect(
    page.getByRole("link", { name: "Continue with Household accounts" }),
  ).toHaveAttribute("href", "/login?return_to=%2Finvite%2F");
});
