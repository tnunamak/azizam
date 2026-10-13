import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

test("remove an app through Apps & keys and invalidate its key", { tag: GLOBAL_STATE_TAG }, async ({
  page,
  request,
}) => {
  const name = "Removable browser client";
  await page.goto("/apps/");
  await page
    .getByRole("button", { name: "New app", exact: true })
    .first()
    .click();
  await page.getByLabel("App name", { exact: true }).fill(name);
  const mintedResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/me/api/apps/") &&
      response.url().endsWith("/keys") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create app and key", exact: true })
    .click();
  const { credential } = await (await mintedResponse).json();
  const reveal = page.getByRole("dialog", { name: `Connect ${name}` });
  await expect(reveal).toBeVisible();
  await reveal.getByRole("button", { name: "I’ve saved it" }).click();
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${credential.api_key}` },
      })
    ).status(),
  ).toBe(200);
  const card = page
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Remove app" }).click();
  const confirmation = page.getByRole("dialog", { name: `Remove ${name}?` });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Cancel" }).click();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Remove app" }).click();
  const deletedResponse = page.waitForResponse(
    (response) =>
      /\/me\/api\/apps\/[^/]+$/.test(new URL(response.url()).pathname) &&
      response.request().method() === "DELETE",
  );
  await confirmation.getByRole("button", { name: "Remove app" }).click();
  expect((await deletedResponse).ok()).toBe(true);
  await expect(confirmation).toHaveCount(0);
  await expect(page.getByRole("heading", { name, exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name, exact: true })).toHaveCount(0);
  expect(
    (
      await request.get("/openai/v1/models", {
        headers: { authorization: `Bearer ${credential.api_key}` },
      })
    ).status(),
  ).toBe(401);
});

test("audio capability never selects an image-only workflow", async ({
  page,
}) => {
  await page.route("**/me/api/models/catalog", (route) =>
    fulfilJson(route, { aliases: [], models: [] }),
  );
  await page.route("**/me/api/workflows", (route) =>
    fulfilJson(route, {
        workflows: [
          {
            id: "image-only",
            title: "Image-only workflow",
            outputs: [{ kind: "image" }],
            inputs: [],
          },
        ],
      }),
  );
  await page.goto("/try/?capability=audio&model=audio");
  await expect(
    page.getByRole("tab", { name: "Workflow", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  // No audio workflow: say so with the owner's next step, never another task.
  await expect(page.getByText("No audio engine is set up.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Add one in Engines" })).toBeVisible();
  await expect(page.getByRole("combobox").first()).not.toContainText(
    "Image-only workflow",
  );
  await expect(
    page.getByRole("button", { name: "Run", exact: true }),
  ).toBeDisabled();
});
