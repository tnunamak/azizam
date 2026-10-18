// Release bar 1 (docs/PRODUCT_STRATEGY.md "Release bar"): a new operator opens
// /setup, adds an engine and gets a chat reply in under ten minutes. The gateway
// is the fresh one of playwright.config.ts (`chromium-first-run`): the shipped
// first-run config, no owner, no engine. Beside it run a stand-in for LiteLLM
// (+1), which sends each request to the address its generated config names, and
// the fake engine the operator adds (+2) (scripts/ui/serve.py, UI_E2E_FIRST_RUN).
// This clock covers what the operator does in the browser. The gate adds the
// Docker Compose bring-up (tests/e2e/fresh_instance.py --compose) to it and fails
// at ten minutes (scripts/ci/journey_clock.py). The test takes `test` from
// Playwright, not from ./fixtures/isolation: that fixture signs in as the lane
// owner, and this server has none until the test creates one.
import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";

const BUDGET_MS = 10 * 60 * 1000;
const PASSWORD = "A long first run test password";
// What the engine the test adds says and lists (scripts/ui/serve.py, UI_E2E_FIRST_RUN).
// The gateway's built-in substrate answers with other words, so this reply can only
// come from the added engine.
const ENGINE_NAME = "Marker engine";
const ENGINE_MODEL = "marker-engine-chat";
const REPLY = "Answered by the engine you just added: marker-engine-chat.";

test("a new operator sets up, adds the fake engine and chats in under ten minutes", async ({
  page,
  baseURL,
}, testInfo) => {
  test.setTimeout(BUDGET_MS + 60_000);
  // +1 is the gateway's substrate stand-in; the engine the operator adds is +2.
  const enginePort = Number(new URL(baseURL!).port) + 2;
  const started = Date.now();

  // 1. /setup: no owner yet, so the page offers to create one.
  await page.goto("/setup/");
  await expect(
    page.getByRole("heading", { name: "Set up your gateway" }),
  ).toBeVisible();
  await page.getByLabel("Email or username").fill("operator@example.test");
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create owner account" }).click();
  await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
  await expect(page.getByRole("heading", { name: "Home", level: 1 })).toBeVisible();

  // 2. Home leads to the next step: no engine yet, so it says where to add one.
  const addEngine = page.getByRole("link", { name: "Add an engine" }).first();
  await expect(addEngine).toBeVisible();
  await addEngine.click();
  await expect(page).toHaveURL(/\/engines\/?$/);

  // 3. The real Add engine dialog: pick a kind, give an address, accept what the
  // gateway found. No JSON anywhere.
  await page.getByRole("button", { name: "Add engine", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Add engine" });
  await dialog.getByRole("button", { name: /Any OpenAI-compatible server/ }).click();
  await dialog.getByLabel(/^Address/).fill(`http://127.0.0.1:${enginePort}/v1`);
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(
    dialog.getByRole("heading", { name: "What this server can do" }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(dialog.getByRole("checkbox", { name: "Chat" })).toBeChecked();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Models and limits" })).toBeVisible();
  await expect(dialog.getByText(ENGINE_MODEL, { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Name and create" })).toBeVisible();
  await dialog.getByLabel(/^Name/).fill(ENGINE_NAME);
  await dialog.getByRole("button", { name: "Add engine", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: ENGINE_NAME })).toBeVisible();

  // 4. Try: send a chat and read the reply.
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Try", exact: true })
    .click();
  await expect(page).toHaveURL(/\/try\/?$/);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  const resultHeading = page.getByRole("heading", { name: "Chat result", exact: true });
  await expect(resultHeading).toBeVisible({ timeout: 60_000 });
  const result = page.locator("div").filter({ has: resultHeading }).last();
  await expect(result.getByText(REPLY, { exact: true })).toBeVisible();

  const elapsedMs = Date.now() - started;
  const seconds = Math.round(elapsedMs / 100) / 10;
  // The gate adds this to the Compose bring-up (scripts/ci/journey_clock.py), also
  // when the journey is too slow: the file is written before the budget check.
  if (process.env.FIRST_RUN_TIMING_OUT)
    writeFileSync(process.env.FIRST_RUN_TIMING_OUT, JSON.stringify({ elapsed_seconds: seconds }) + "\n");
  testInfo.annotations.push({ type: "first-run-seconds", description: String(seconds) });
  await testInfo.attach("first-run-timing.json", {
    body: JSON.stringify({ elapsed_seconds: seconds, budget_seconds: BUDGET_MS / 1000 }),
    contentType: "application/json",
  });
  console.log(`first-run journey: ${seconds} s of ${BUDGET_MS / 1000} s`);

  // Attribution: the reply came from the engine just added, and the receipt says so.
  const registry = await (await page.request.get("/admin/api/operator/service-registry")).json();
  const added = Object.values(registry.service_registry.services as Record<string, ServiceRow>).filter(
    (service) => service.source === "ui",
  );
  expect(added.map((service) => service.name)).toEqual([ENGINE_NAME]);
  expect(added[0].config.address).toBe(`http://127.0.0.1:${enginePort}/v1`);
  const publicName = added[0].config.models.find((model) => model.upstream_model === ENGINE_MODEL)?.model_group;
  expect(publicName, "the added engine registers its model").toBeTruthy();
  const receiptHref = await page.getByRole("link", { name: "View receipt" }).getAttribute("href");
  const receiptId = new URL(receiptHref!, baseURL).searchParams.get("receipt");
  const receipt = await (await page.request.get(`/me/api/receipts/${receiptId}`)).json();
  expect(receipt).toMatchObject({ status: "succeeded", requested_model: publicName, resolved_model: publicName });
  expect(receipt.resolved_model).not.toBe("demo");

  expect(elapsedMs, `the first-run journey took ${seconds} s`).toBeLessThan(BUDGET_MS);
});

type ServiceRow = {
  name: string;
  source: string;
  config: { address: string; models: { model_group: string; upstream_model: string }[] };
};
