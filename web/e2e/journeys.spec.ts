import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

test("create, rotate and revoke an app key, then inspect its audit receipt", { tag: GLOBAL_STATE_TAG }, async ({
  page,
  request,
}) => {
  const name = "Journey client";
  await page.goto("/apps/");
  await page
    .getByRole("button", { name: "New app", exact: true })
    .first()
    .click();
  await page.getByLabel("App name", { exact: true }).fill(name);
  // There is no app recipe to choose: the name picks the setup guide.
  await expect(page.getByRole("combobox", { name: "App recipe" })).toHaveCount(0);
  const mintedResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/me/api/apps/") &&
      response.url().endsWith("/keys") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create app and key", exact: true })
    .click();
  const { credential: firstKey } = await (await mintedResponse).json();
  const reveal = page.getByRole("dialog", { name: `Connect ${name}` });
  await expect(reveal).toBeVisible();
  // The setup guide shows the app's fields; the key stays in its own field.
  await expect(reveal.getByRole("heading", { name: "Set up your app" })).toBeVisible();
  await expect(reveal.getByRole("combobox", { name: "Your app" })).toBeVisible();
  await expect(reveal.getByLabel("Your key", { exact: true })).toHaveValue(firstKey.api_key);
  expect(
    await reveal.locator("section input").evaluateAll(
      (inputs, key) => inputs.some((input) => (input as HTMLInputElement).value === key),
      firstKey.api_key,
    ),
  ).toBe(false);
  await page.keyboard.press("Escape");
  await expect(reveal).toBeVisible();
  await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
  await expect(reveal).not.toBeVisible();
  await page.reload();
  const card = page
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await expect(card).toContainText("1 key");
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Rotate keys" }).click();
  const replacementResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/me/api/apps/") &&
      response.url().endsWith("/keys") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create replacement key" }).click();
  const { credential: replacement } = await (await replacementResponse).json();
  // Rotation does not interrupt clients before the replacement is saved.
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${firstKey.api_key}` },
      })
    ).ok(),
  ).toBe(true);
  await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
  await expect(reveal).not.toBeVisible();
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${firstKey.api_key}` },
      })
    ).status(),
  ).toBe(401);
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${replacement.api_key}` },
      })
    ).ok(),
  ).toBe(true);
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Revoke keys" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Revoke keys", exact: true })
    .click();
  await expect(card).toContainText("0 keys");
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${replacement.api_key}` },
      })
    ).status(),
  ).toBe(401);

  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  const filterRequest = page.waitForResponse((response) =>
    response.url().includes("action_prefix=app."),
  );
  await page.getByRole("combobox", { name: "Change type" }).click();
  await page.getByRole("option", { name: "Apps", exact: true }).click();
  const filtered = await (await filterRequest).json();
  expect(filtered.records.length).toBeGreaterThan(0);
  expect(
    filtered.records.every((record: { action: string }) =>
      record.action.startsWith("app."),
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: /Details: Created an app/ })
    .first()
    .click();
  const detail = page.getByRole("dialog");
  await expect(detail).toBeVisible();
  await detail.getByText("Technical details", { exact: true }).click();
  await expect(detail).toContainText("app.self_create");
  await expect(detail).toContainText(firstKey.app_id);
});

test("an owner gives a new app another access plan before its key exists, then changes it", { tag: GLOBAL_STATE_TAG }, async ({
  page,
}) => {
  const name = "Plan journey app";
  const calls: string[] = [];
  page.on("request", (request) => {
    const url = request.url();
    if (request.method() === "PATCH" && /\/admin\/api\/apps\/[^/]+$/.test(url))
      calls.push("set plan");
    if (request.method() === "POST" && /\/me\/api\/apps\/[^/]+\/keys$/.test(url))
      calls.push("mint key");
  });
  await page.goto("/apps/");
  await page.getByRole("button", { name: "New app", exact: true }).first().click();
  await page.getByLabel("App name", { exact: true }).fill(name);
  const dialog = page.getByRole("dialog", { name: "New app" });
  await dialog.getByRole("combobox", { name: "Access plan" }).click();
  await page.getByRole("option", { name: "Local chat", exact: true }).click();
  await dialog.getByRole("button", { name: "Create app and key", exact: true }).click();
  const reveal = page.getByRole("dialog", { name: `Connect ${name}` });
  await expect(reveal).toBeVisible();
  await expect(reveal).toContainText("Access plan “Local chat”");
  expect(calls).toEqual(["set plan", "mint key"]);
  await reveal.getByRole("button", { name: "I’ve saved the key" }).click();
  await expect(reveal).not.toBeVisible();
  const card = page
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await expect(card).toContainText("Access plan: Local chat");

  // The owner can change it later; the app then follows its person again.
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Change access plan" }).click();
  const change = page.getByRole("dialog", { name: `Access plan for ${name}` });
  await expect(change.getByRole("combobox", { name: "Access plan" })).toContainText("Local chat");
  await change.getByRole("combobox", { name: "Access plan" }).click();
  await page.getByRole("option", { name: /^Same as you/ }).click();
  await change.getByRole("button", { name: "Save", exact: true }).click();
  await expect(change).not.toBeVisible();
  await expect(card).not.toContainText("Access plan: Local chat");
});

test("apps explain an empty account and recover after an API error", async ({
  page,
}) => {
  await page.route("**/me/api/session", async (route) => {
    const response = await route.fetch();
    const actual = await response.json();
    await fulfilJson(route, { ...actual, apps: [] }, { response });
  });
  await page.goto("/apps/");
  await expect(
    page.getByText("Connect your first app", { exact: true }),
  ).toBeVisible();
  await page.unroute("**/me/api/session");
  let sessionCalls = 0;
  await page.route("**/me/api/session", (route) => {
    // Keep the shell's viewer request real; fail the Apps request that follows.
    sessionCalls += 1;
    if (sessionCalls === 1) return route.continue();
    return fulfilJson(route, { error: { message: "Unavailable for this test" } }, { status: 503 });
  });
  await page.reload();
  await expect(
    page.getByText("Your apps could not be loaded. Try again."),
  ).toBeVisible();
  await page.unroute("**/me/api/session");
  await page.getByRole("button", { name: /try again|retry/i }).click();
  await expect(
    page.getByRole("heading", { name: "Browser test app" }),
  ).toBeVisible();
});

test("audit has a useful empty filter state and recovers after an API error", async ({
  page,
}) => {
  await page.goto("/activity/");
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  // OWNER-05: people are picked by name; raw IDs live under a technical disclosure.
  await page.getByText("Technical: filter by person ID", { exact: true }).click();
  await page
    .getByRole("textbox", { name: "Technical person ID" })
    .fill("no-such-test-actor");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(
    page.getByText("No matching changes", { exact: true }),
  ).toBeVisible();
  await page.route("**/admin/api/audit?**", (route) =>
    fulfilJson(route, { error: { message: "Unavailable for this test" } }, { status: 503 }),
  );
  await page.reload();
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  await expect(
    page.getByText("Couldn't load changes"),
  ).toBeVisible();
  await page.unroute("**/admin/api/audit?**");
  await page.getByRole("button", { name: /try again|retry/i }).click();
  await expect(
    page.getByRole("button", { name: /Details:/ }).first(),
  ).toBeVisible();
});
