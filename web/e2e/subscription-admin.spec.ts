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

test.describe.serial("subscription administration", () => {

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

test("saved plan detail links persist a selected route order", async ({ page, baseURL }) => {
  // The preceding journey leaves the plan disconnected; its saved detail link still opens.
  await page.goto("/providers/?subscription=test-plan");
  await expect(page.getByRole("heading", { name: ROW })).toBeVisible();
  await expect(page.getByRole("group", { name: "Through Azizam" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: ROW })).toBeVisible();
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Connect ${ROW}` });
  const startedResponse = page.waitForResponse((response) => response.url().endsWith("/admin/api/subscriptions/test-plan/connect") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "Open Test Plan sign-in" }).click();
  const started = (await (await startedResponse).json()).subscription;
  const authorize = new URL(started.authorize_url);
  await dialog.getByLabel("Address from the browser bar").fill(`${started.redirect_uri}?code=fake-${authorize.searchParams.get("nonce")}&state=${authorize.searchParams.get("state")}`);
  await dialog.getByRole("button", { name: "Finish" }).click();
  await expect(dialog.getByText("Connected as owner@example.test.")).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  const session = await (await page.request.get("/admin/api/session")).json();
  const registry = await page.request.get("/admin/api/operator/service-registry");
  const added = await page.request.post("/admin/api/operator/services", {
    headers: { "x-azizam-csrf": session.actor.csrf_token, "If-Match": registry.headers().etag },
    data: { id: "paid-fixture", name: "Paid fixture", base_url: `http://127.0.0.1:${Number(new URL(baseURL!).port) + 1}/v1`,
      remote_provider: true, provider_id: "fixture-paid", api_key_env: "AZIZAM_FIXTURE_PAID_KEY", capabilities: ["chat"],
      models: [{ id: "fixture-wire", public: "fixture-wire", upstream: "plan-model-1", execution_only: true }] },
  });
  expect(added.ok(), await added.text()).toBe(true);
  await page.getByRole("button", { name: "Route order for plan-model-1", exact: true }).click();
  const group = page.getByRole("group", { name: "Route order for plan-model-1", exact: true });
  await expect(group.getByRole("option", { name: "Paid fixture · plan-model-1 · paid", exact: true })).toBeAttached();
  await group.getByLabel("Add route for plan-model-1").selectOption({ label: "Paid fixture · plan-model-1 · paid" });
  await group.getByRole("button", { name: "Move Paid fixture up" }).click();
  await group.getByRole("button", { name: "Save routes" }).click();
  await expect(group.getByText("Routes saved")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Route order for plan-model-1", exact: true }).click();
  await expect(page.getByText("Current saved order: Paid fixture → test-plan")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Move Paid fixture down" })).toBeVisible();
  await page.getByRole("link", { name: "Back to Providers" }).click();
  await expect(page.getByRole("heading", { name: "Providers", exact: true })).toBeVisible();
  await page.goto("/providers/?subscription=test-plan-second");
  await expect(page.getByRole("heading", { name: "Second Plan subscription" })).toBeVisible();
});

});
