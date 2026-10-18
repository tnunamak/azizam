// Release bar 1 (docs/PRODUCT_STRATEGY.md "Release bar"): a new operator opens
// /setup, adds an engine and gets a chat reply in under ten minutes. The gateway
// is the fresh one of playwright.config.ts (`chromium-first-run`): the shipped
// first-run config, no owner, no engine, and the fixture's fake engine one port
// above it. The clock covers what the operator does in the browser; starting
// the process is the fresh-instance journey's job (tests/e2e/fresh_instance.py,
// including Docker Compose). The test takes `test` from Playwright, not from
// ./fixtures/isolation: that fixture signs in as the lane owner, and this
// server has none until the test creates one.
import { expect, test } from "@playwright/test";

const BUDGET_MS = 10 * 60 * 1000;
const PASSWORD = "A long first run test password";
const REPLY =
  "Your gateway is connected. This is a demonstration response from the isolated capture engine.";

test("a new operator sets up, adds the fake engine and chats in under ten minutes", async ({
  page,
  baseURL,
}, testInfo) => {
  test.setTimeout(BUDGET_MS + 60_000);
  const enginePort = Number(new URL(baseURL!).port) + 1;
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
  await expect(dialog.getByText("demo-chat", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Continue" }).click();
  await expect(dialog.getByRole("heading", { name: "Name and create" })).toBeVisible();
  await dialog.getByRole("button", { name: "Add engine", exact: true }).click();
  await expect(page.getByText(/^Added “.*”\. It is on, with Chat/)).toBeVisible();

  // 4. Try: send a chat and read the reply.
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Try", exact: true })
    .click();
  await expect(page).toHaveURL(/\/try\/?$/);
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Chat result", { exact: true })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByText(REPLY, { exact: true })).toBeVisible();

  const elapsedMs = Date.now() - started;
  const seconds = Math.round(elapsedMs / 100) / 10;
  testInfo.annotations.push({ type: "first-run-seconds", description: String(seconds) });
  await testInfo.attach("first-run-timing.json", {
    body: JSON.stringify({ elapsed_seconds: seconds, budget_seconds: BUDGET_MS / 1000 }),
    contentType: "application/json",
  });
  console.log(`first-run journey: ${seconds} s of ${BUDGET_MS / 1000} s`);
  expect(elapsedMs, `the first-run journey took ${seconds} s`).toBeLessThan(BUDGET_MS);
});
