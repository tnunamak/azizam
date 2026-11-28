// The owner's whole subscription journey on the Providers page, against the real fixture gateway: no stubbed
// subscription route. The gateway's kind "Test Plan" is data (scripts/ui/fixture_subscription_kind.yml) that points
// at a fake plan provider the fixture serves itself (scripts/ui/fake_plan_upstream.py); no real provider host is
// involved. This server is its own project (playwright.config.ts), so no other spec sees the kind.
import { request, type Page } from "@playwright/test";
import { test, expect } from "./fixtures/isolation";

// The fake provider speaks TLS with a throwaway certificate (the new tab opened by the sign-in button reaches it).
test.use({ ignoreHTTPSErrors: true });

const ROW = "Test Plan subscription";

// The state word is rendered twice in a row (one copy for each layout width), so look at the visible one.
const stateWord = (page: Page, word: string) => row(page).getByText(word, { exact: true }).filter({ visible: true });

function row(page: Page) {
  return page
    .getByRole("region", { name: "Subscriptions" })
    .getByRole("listitem")
    .filter({ hasText: ROW });
}

async function setProviderModels(baseURL: string, count: number) {
  // The fake provider sits two ports above the gateway.
  const upstream = `https://127.0.0.1:${Number(new URL(baseURL).port) + 2}`;
  const api = await request.newContext({ ignoreHTTPSErrors: true });
  const answer = await api.post(`${upstream}/__fixture/models`, { data: { count } });
  expect(answer.ok()).toBe(true);
  await api.dispose();
}

test("the owner connects, checks and disconnects a subscription end to end", async ({ page, baseURL }) => {
  await setProviderModels(baseURL!, 2);
  await page.goto("/providers/");

  // 1. Nothing is connected: the kind still has a row, Off, with Connect.
  await expect(stateWord(page, "Off")).toBeVisible();
  await expect(row(page).getByRole("button", { name: `Connect ${ROW}` })).toBeVisible();

  // 2. Connect: the dialog calls the real connect route; the test reads the sign-in address from its answer.
  await row(page).getByRole("button", { name: `Connect ${ROW}` }).click();
  const dialog = page.getByRole("dialog", { name: `Connect ${ROW}` });
  const connect = page.waitForResponse(
    (response) => response.url().endsWith("/admin/api/subscriptions/test-plan/connect") && response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Open Test Plan sign-in" }).click();
  const started = (await (await connect).json()).subscription as { authorize_url: string; redirect_uri: string };
  const authorize = new URL(started.authorize_url);
  expect(authorize.origin).toBe(`https://127.0.0.1:${Number(new URL(baseURL!).port) + 2}`);
  const state = authorize.searchParams.get("state")!;
  const nonce = authorize.searchParams.get("nonce")!;

  // The browser would end on the redirect address; the fake provider takes the nonce back inside the code.
  const callback = `${started.redirect_uri}?code=fake-${nonce}&state=${state}`;
  await dialog.getByLabel("Address from the browser bar").fill(callback);
  await dialog.getByRole("button", { name: "Finish" }).click();
  await expect(dialog.getByText("Connected as owner@example.test.")).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();

  // The row turns Ready with the account and the model count (the hidden model is not counted).
  await expect(stateWord(page, "Ready")).toBeVisible();
  await expect(row(page).getByText(/owner@example\.test · 2 models/)).toBeVisible();

  // The state is the gateway's, not the page's: a fresh load shows it too.
  await page.reload();
  await expect(row(page).getByText(/owner@example\.test · 2 models/)).toBeVisible();

  // 3. Check now: the provider's list grew; the row reads the new count and stays Ready.
  await setProviderModels(baseURL!, 3);
  const refresh = page.waitForResponse(
    (response) => response.url().endsWith("/admin/api/subscriptions/test-plan/refresh-models") && response.request().method() === "POST",
  );
  await row(page).getByRole("button", { name: `Actions for ${ROW}` }).click();
  await page.getByRole("menuitem", { name: "Check now" }).click();
  expect((await refresh).ok()).toBe(true);
  await expect(row(page).getByText(/owner@example\.test · 3 models/)).toBeVisible();
  await expect(stateWord(page, "Ready")).toBeVisible();

  // 4. Disconnect: typed name, then the row is Off with Connect again.
  await row(page).getByRole("button", { name: `Actions for ${ROW}` }).click();
  await page.getByRole("menuitem", { name: "Disconnect" }).click();
  const confirm = page.getByRole("alertdialog").or(page.getByRole("dialog", { name: `Disconnect ${ROW}?` })).first();
  const disconnect = confirm.getByRole("button", { name: "Disconnect" });
  await expect(disconnect).toBeDisabled();
  await confirm.getByRole("textbox").fill(ROW);
  await disconnect.click();
  await expect(stateWord(page, "Off")).toBeVisible();
  await expect(row(page).getByRole("button", { name: `Connect ${ROW}` })).toBeVisible();
  await expect(row(page).getByText("Disconnected. Connect again to use its models.")).toBeVisible();
});
