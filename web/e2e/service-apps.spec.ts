import { expect, test } from "./fixtures/isolation";
import type { Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";

// Service apps (docs: designs/service-apps.md). An administrator makes a service app in one request; a member never
// sees the choice; the owner can hand a service app to someone else from the People confirmations.
const SHOTS = process.env.SERVICE_APPS_SHOTS_DIR ?? path.join(os.tmpdir(), "service-apps-shots");
// Capture after dialog and menu fades end, so the picture shows the settled page.
async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  await page.waitForFunction(() => document.getAnimations().length === 0);
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}
const VIEWPORT = { width: 1280, height: 800 };

async function csrf(page: Page): Promise<string> {
  const snapshot = await (await page.request.get("/me/api/session")).json();
  return snapshot.csrf_token as string;
}

test("an administrator makes a service app in one request and finds it under Service apps", async ({ page, isolated }) => {
  await page.setViewportSize(VIEWPORT);
  const personal = isolated.name("Phone");
  const service = isolated.name("Door camera");
  await page.goto("/apps/");

  // A personal app first, so the page has both sections.
  await page.getByRole("button", { name: "New app" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New app" });
  await dialog.getByLabel("App name").fill(personal);
  // Nothing is preselected: the choice is required.
  await expect(dialog.getByRole("radio", { name: /^You/ })).not.toBeChecked();
  await expect(dialog.getByRole("radio", { name: /^A service/ })).not.toBeChecked();
  await expect(dialog.getByRole("button", { name: "Create app and key" })).toBeDisabled();
  await dialog.getByRole("radio", { name: /^You/ }).click();
  await shot(page, "new-app-acts-as-you");
  await dialog.getByRole("button", { name: "Create app and key" }).click();
  await page.getByRole("dialog", { name: /^New key for / }).getByRole("button", { name: "I’ve saved the key" }).click();

  // The service app: one request carries the kind and the plan.
  const created = page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname === "/me/api/apps");
  await page.getByRole("button", { name: "New app" }).first().click();
  const second = page.getByRole("dialog", { name: "New app" });
  await second.getByLabel("App name").fill(service);
  await second.getByRole("radio", { name: /^A service/ }).click();
  await expect(second.getByText(/No person is behind it, and it keeps no prompt memory/)).toBeVisible();
  await second.getByRole("combobox", { name: "Access plan" }).click();
  await page.getByRole("option", { name: "Local chat" }).click();
  await shot(page, "new-app-acts-as-service");
  await second.getByRole("button", { name: "Create app and key" }).click();
  const body = (await created).postDataJSON();
  expect(body).toMatchObject({ name: service, kind: "service" });
  expect(body.access_plan).not.toBe("person");

  // The key is shown once, and only as the first key under the chosen plan.
  const reveal = page.getByRole("dialog", { name: `New key for ${service}` });
  await expect(reveal.getByRole("textbox", { name: "Your key" })).toHaveValue(/^opk_/);
  await reveal.getByRole("button", { name: "I’ve saved the key" }).click();

  const yours = page.getByRole("list", { name: "Your apps" });
  const services = page.getByRole("list", { name: "Service apps" });
  await expect(yours.getByRole("heading", { name: personal })).toBeVisible();
  await expect(services.getByRole("heading", { name: service })).toBeVisible();
  await expect(yours.getByRole("heading", { name: service })).toHaveCount(0);
  await expect(services).toContainText("Answers to");
  await expect(services).toContainText("No prompt memory");
  await expect(services).toContainText("Local chat");
  await shot(page, "apps-two-sections");
});

test("a member sees no 'Acts as' choice and sends only a name", async ({ isolated }) => {
  const member = await isolated.member({ name: "Plain member" });
  const page = member.page;
  await page.setViewportSize(VIEWPORT);
  await page.goto("/apps/");
  const created = page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname === "/me/api/apps");
  await page.getByRole("button", { name: "New app" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New app" });
  await expect(dialog.getByRole("radio")).toHaveCount(0);
  await expect(dialog.getByText("Acts as")).toHaveCount(0);
  await dialog.getByLabel("App name").fill(isolated.name("Laptop"));
  await dialog.getByRole("button", { name: "Create app and key" }).click();
  expect((await created).postDataJSON()).toEqual({ name: expect.any(String) });
  await expect(page.getByRole("dialog", { name: /^New key for / })).toBeVisible();
  // A member's own request for a service app is refused by the server, not only hidden by the page.
  const refused = await page.request.post("/me/api/apps", {
    headers: { "x-azizam-csrf": await csrf(page), "x-azizam-user-action": "1" },
    data: { name: isolated.name("Sneaky"), kind: "service" },
  });
  expect(refused.status()).toBe(403);
});

test("the remove confirmation lists a member's service apps and moves one to another person", async ({ page, isolated }) => {
  await page.setViewportSize(VIEWPORT);
  const member = await isolated.member({ name: "Leaving member" });
  const service = isolated.name("Hall sensor");
  // The owner makes a service app that answers to the member, in one request.
  const made = await page.request.post("/me/api/apps", {
    headers: { "x-azizam-csrf": await csrf(page), "x-azizam-user-action": "1" },
    data: { name: service, kind: "service", access_plan: "person", owner_user_id: member.userId },
  });
  expect(made.status(), await made.text()).toBe(201);

  await page.goto("/people/");
  await page.getByRole("button", { name: /^Actions for .*Leaving member/ }).click();
  await page.getByRole("menuitem", { name: "Remove person" }).click();
  const removal = page.getByRole("dialog", { name: "Remove person?" });
  const list = removal.getByRole("list", { name: "Service apps that will stop" });
  await expect(list).toContainText(service);
  await shot(page, "remove-person-confirmation");

  await list.getByRole("button", { name: "Move to another person first" }).first().click();
  const move = page.getByRole("dialog", { name: `Who does ${service} answer to?` });
  await move.getByRole("combobox", { name: "Answers to" }).click();
  // Not the person who leaves, and the owner is a choice.
  await expect(page.getByRole("option", { name: /Leaving member/ })).toHaveCount(0);
  await page.getByRole("option").first().click();
  await expect(move).toContainText("will run under");
  await move.getByRole("button", { name: "Move app" }).click();
  await expect(move).toHaveCount(0);
  await expect(list.getByText(service)).toHaveCount(0);
  await removal.getByRole("button", { name: "Cancel" }).click();
});
