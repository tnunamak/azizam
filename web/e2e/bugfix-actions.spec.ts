import { test, expect } from "@playwright/test";

test("remove an app through Apps & keys and invalidate its key", async ({
  page,
  request,
}) => {
  const name = "Removable browser client";
  await page.goto("/ui/apps/");
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
  await page.getByRole("button", { name: "Create app", exact: true }).click();
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
    .getByRole("article")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Remove app" }).click();
  const confirmation = page.getByRole("dialog", { name: `Remove ${name}?` });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Cancel" }).click();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Remove app" }).click();
  await confirmation.getByRole("button", { name: "Remove app" }).click();
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

test("Showcase handles real structured request shapes in the browser", async ({
  page,
}) => {
  const exceptions: string[] = [];
  page.on("pageerror", (error) => exceptions.push(error.message));
  await page.route("**/showcase/examples/index.json", (route) =>
    route.fulfill({
      json: {
        data: [
          {
            id: "structured",
            base: "/showcase/examples/structured/",
            card: "chat",
            title: "Recorded structured request",
            inputs: [
              {
                value: {
                  model: "fast",
                  max_tokens: 32,
                  messages: [{ role: "user", content: "Hello" }],
                },
              },
            ],
            outputs: [{ kind: "text", text: "Recorded answer" }],
          },
        ],
      },
    }),
  );
  await page.goto("/ui/showcase/");
  await expect(
    page.getByText("Recorded answer", { exact: true }),
  ).toBeVisible();
  await page.getByText("Recorded input", { exact: true }).click();
  await expect(page.locator("pre")).toContainText('"model": "fast"');
  await expect(page.locator("pre")).toContainText('"messages": [');
  expect(exceptions).toEqual([]);
});
