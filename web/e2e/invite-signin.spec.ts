import { expect, test } from "@playwright/test";

test("invited member creates a password account and signs in again", async ({
  browser,
  page,
  baseURL,
}) => {
  const session = await (await page.request.get("/admin/api/session")).json();
  const plans = await (await page.request.get("/admin/api/access-plans")).json();
  const plan = plans.access_plans.find(
    (item: { name: string }) => item.name === "Local chat",
  );
  expect(plan).toBeDefined();
  const inviteResponse = await page.request.post("/admin/api/invites", {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
    data: { access_plan: plan.id, email: "invited@example.test" },
  });
  expect(inviteResponse.ok()).toBe(true);
  const invite = await inviteResponse.json();

  const guest = await browser.newContext({
    baseURL,
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

    const returnVisit = await browser.newContext({ baseURL });
    try {
      const signIn = await returnVisit.newPage();
      await signIn.goto("/sign-in/");
      await signIn
        .getByLabel("Email", { exact: true })
        .fill("invited@example.test");
      await signIn
        .getByLabel("Password", { exact: true })
        .fill("A long browser test password");
      await signIn
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
      await expect(signIn).toHaveURL(/^https?:\/\/[^/]+\/$/);
    } finally {
      await returnVisit.close();
    }
  } finally {
    await guest.close();
  }
});
