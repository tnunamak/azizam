// Release bar 2 (docs/PRODUCT_STRATEGY.md "Release bar"): the owner invites a
// member, and the member connects an app with copy-paste settings and no help
// from the admin. The owner invites through People. The member accepts, signs
// in, makes an app and key, and copies the key, the address and the model name
// out of the screens. A plain request shaped like an OpenAI SDK call, built from
// exactly those copied values, must then succeed. Its receipt names the member's
// app, and the owner's Activity names the member. The member's browser never
// opens an owner page or calls an owner API.
import { request as playwrightRequest } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

const PASSWORD = "A long member connect password";
// What the member's pages may load: no People, Engines, Settings or other owner page.
const MEMBER_PAGES = new Set(["/invite/", "/", "/connect/", "/apps/"]);

test("an invited member connects an app with copied settings and the receipt names them", async ({
  page,
  browser,
  baseURL,
  isolated,
}) => {
  const email = isolated.email("connector");
  const memberName = isolated.name("Connector");

  // The owner invites through People, as an owner would.
  await page.goto("/people/");
  await page.getByRole("button", { name: "Invite someone", exact: true }).click();
  const invite = page.getByRole("dialog");
  await invite.getByRole("combobox", { name: "Access plan", exact: true }).click();
  await page.getByRole("option", { name: "Local chat" }).click();
  await invite.getByLabel("Email (optional)").fill(email);
  await invite.getByRole("button", { name: "Create invite link" }).click();
  await expect(invite).toContainText("Invitation ready");
  const inviteLink = new URL(
    await invite.getByLabel("Link", { exact: true }).inputValue(),
    baseURL,
  ).toString();
  await invite.getByRole("button", { name: "I’ve saved the link" }).click();

  // From here on the member works alone, in a browser of their own.
  const member = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const pages = new Set<string>();
  const ownerCalls: string[] = [];
  member.on("request", (request) => {
    const url = new URL(request.url());
    // The shell asks who is signed in on every page, whoever they are.
    if (url.pathname.startsWith("/admin") && url.pathname !== "/admin/api/session")
      ownerCalls.push(`${request.method()} ${url.pathname}`);
    if (request.isNavigationRequest() && request.frame().parentFrame() === null)
      pages.add(url.pathname);
  });
  try {
    const mine = await member.newPage();
    await mine.goto(inviteLink);
    await mine.getByLabel("Your name").fill(memberName);
    await mine.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await mine.getByLabel("Confirm password").fill(PASSWORD);
    await mine.getByRole("button", { name: "Create account" }).click();
    await expect(mine.getByRole("heading", { name: "Your account is ready" })).toBeVisible();
    await mine.getByRole("button", { name: "I’ve saved it" }).click();
    await expect(mine).toHaveURL(/^https?:\/\/[^/]+\/$/);

    // Connect leads to a new key; the key is shown once, with a copy button.
    await mine
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Connect", exact: true })
      .click();
    await expect(mine.getByRole("heading", { level: 1, name: "Connect" })).toBeVisible();
    await mine.getByRole("link", { name: "Create a key" }).click();
    await expect(mine).toHaveURL(/\/apps\/?\?action=create-key/);
    const create = mine.getByRole("dialog");
    await create.getByLabel("App name").fill("My chat app");
    await create.getByRole("button", { name: "Create app and key" }).click();
    const reveal = mine.getByRole("dialog", { name: /^Connect / });
    await expect(reveal.getByLabel("Your key", { exact: true })).toHaveValue(/^opk_/);
    await reveal.getByRole("button", { name: "Copy your key" }).click();
    const key = await mine.evaluate(() => navigator.clipboard.readText());
    expect(key).toMatch(/^opk_/);
    await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
    await expect(reveal).toBeHidden();

    // Connect has the address and the model name, each with a copy button.
    await mine
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Connect", exact: true })
      .click();
    await expect(mine.getByRole("heading", { level: 1, name: "Connect" })).toBeVisible();
    await mine.getByRole("button", { name: "Copy URL", exact: true }).click();
    const baseUrl = await mine.evaluate(() => navigator.clipboard.readText());
    await mine.getByRole("button", { name: "Copy Model IDs", exact: true }).click();
    const model = await mine.evaluate(() => navigator.clipboard.readText());
    expect(baseUrl).toBe(`${baseURL}/openai/v1`);
    expect(model.trim(), "the copied model name").not.toBe("");

    // The copied values, in a request no browser session helps: no cookies, the
    // shape an OpenAI SDK sends.
    const sdk = await playwrightRequest.newContext({ storageState: { cookies: [], origins: [] } });
    try {
      const answer = await sdk.post(`${baseUrl}/chat/completions`, {
        headers: {
          authorization: `Bearer ${key}`,
          "user-agent": "OpenAI/Python 1.40.0",
        },
        data: {
          model,
          messages: [{ role: "user", content: isolated.tag("Say hello") }],
        },
      });
      expect(answer.status(), await answer.text()).toBe(200);
      const body = await answer.json();
      expect(body.choices[0].message.content).toEqual(expect.any(String));
      const receiptId = answer.headers()["x-azizam-receipt-id"];
      expect(receiptId).toBeTruthy();

      // The member reads the receipt of their own request: it names their app.
      const receiptResponse = await member.request.get(`/me/api/receipts/${receiptId}`);
      expect(receiptResponse.ok()).toBe(true);
      const detail = await receiptResponse.json();
      const receipt = detail.receipt ?? detail;
      expect(receipt.request_id).toBe(receiptId);
      expect(receipt.status).toBe("succeeded");
      expect(receipt.source).toMatchObject({ kind: "app", label: "My chat app" });

      // The owner's Activity names the person behind that app.
      const activity = await page.request.get(
        `/admin/api/activity/requests?source=app:${receipt.source.app_id}`,
      );
      expect(activity.ok()).toBe(true);
      const row = ((await activity.json()).records as { receipt: { request_id: string }; person_label: string }[]).find(
        (record) => record.receipt.request_id === receiptId,
      );
      expect(row?.person_label).toBe(memberName);
    } finally {
      await sdk.dispose();
    }

    expect(ownerCalls, "the member's browser called an owner API").toEqual([]);
    expect([...pages].filter((path) => !MEMBER_PAGES.has(path))).toEqual([]);
  } finally {
    await member.close();
  }
});
