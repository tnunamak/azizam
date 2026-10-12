import { expect, test } from "@playwright/test";

test("invited member creates a password account and signs in again", async ({
  browser,
  page,
  baseURL,
}) => {
  const session = await (await page.request.get("/admin/api/session")).json();
  const profiles = await (await page.request.get("/admin/api/profiles")).json();
  const profile = profiles.profiles.find(
    (item: { name: string }) => item.name === "Local chat",
  );
  expect(profile).toBeDefined();
  const inviteResponse = await page.request.post("/admin/api/invites", {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
    data: { profile_id: profile.id, email: "invited@example.test" },
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
    await expect(firstVisit).toHaveURL(/\/$/);

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
      await expect(signIn).toHaveURL(/\/$/);
    } finally {
      await returnVisit.close();
    }
  } finally {
    await guest.close();
  }
});
