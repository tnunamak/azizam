import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// The Add engine journey (add-engine-spec.md sections 2 and 9): add a chat
// engine with a key against the fixture gateway's fake engine, then edit its
// address on the engine page and see that the saved key will not be sent to
// the new address (N2). The last step and the engine page also answer "Who can
// use these models", computed by the gateway from the fixture's access plans. The fixture lets the gateway reach only 127.0.0.1, and
// the seed already serves /v1 there, so the engine goes in at /provider/v1.
// It writes the service registry, so it runs in the serial global-state lane
// and removes the engine it added.

test.use({ contextOptions: { reducedMotion: "reduce" } });

for (const width of [1280, 390]) {
  test(`add a chat engine, then edit its address at ${width}`, { tag: GLOBAL_STATE_TAG }, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    const enginePort = Number(new URL(baseURL!).port) + 1;
    const name = `Journey engine ${width}`;
    let serviceId: string | null = null;
    try {
      await page.goto("/engines/");
      await page.getByRole("button", { name: "Add engine", exact: true }).first().click();
      const dialog = page.getByRole("dialog", { name: "Add engine" });
      await expect(dialog.getByText(/Step 1 of 5/)).toBeVisible();
      await dialog.getByRole("button", { name: /Any OpenAI-compatible server/ }).click();

      await expect(dialog.getByText(/Step 2 of 5/)).toBeVisible();
      // The seed already serves this address: Continue says so before any request.
      await dialog.getByLabel(/^Address/).fill(`http://127.0.0.1:${enginePort}/v1`);
      await dialog.getByRole("button", { name: "Continue" }).click();
      await expect(dialog.getByText("Already added")).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Open Local test engine" })).toBeVisible();
      // The fake engine answers the same OpenAI routes under /provider/v1 too.
      const address = `http://127.0.0.1:${enginePort}/provider/v1`;
      await dialog.getByLabel(/^Address/).fill(address);
      await expect(dialog.getByText(`${address}/models`)).toBeVisible();
      await dialog.getByLabel(/^API key/).fill("sk-journey-local-key");
      await dialog.getByRole("button", { name: "Continue" }).click();

      await expect(dialog.getByRole("heading", { name: "What this server can do" })).toBeVisible();
      await expect(dialog.getByText(/Reached the server\. Key accepted\./)).toBeVisible();
      const chat = dialog.getByRole("checkbox", { name: "Chat" });
      await chat.check();
      await expect(chat).toBeChecked();
      await dialog.getByRole("button", { name: "Continue" }).click();

      await expect(dialog.getByRole("heading", { name: "Models and limits" })).toBeVisible();
      await expect(dialog.getByText("anthropic/claude-sonnet-4.5")).toBeVisible();
      await dialog.getByRole("button", { name: "Continue" }).click();

      await expect(dialog.getByRole("heading", { name: "Name and create" })).toBeVisible();
      await dialog.getByLabel(/^Name/).fill(name);
      // Who can use the models: the gateway's answer, shown before Create. The
      // fixture plans allow all chat models and the signed-in owner holds one.
      await expect(dialog.getByRole("heading", { name: "Who can use these models" })).toBeVisible();
      await expect(dialog.getByText(/^Usable by:/)).toBeVisible();
      await expect(dialog.getByRole("link", { name: "Access plans" })).toBeVisible();
      await expect(dialog.getByRole("checkbox", { name: /who can use/i })).toHaveCount(0);
      if (width === 390) {
        const box = await dialog.getByRole("link", { name: "Access plans" }).boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(44);
      }
      const created = page.waitForResponse(
        (response) => response.request().method() === "POST" && response.url().endsWith("/admin/api/operator/services"),
      );
      await dialog.getByRole("button", { name: "Add engine", exact: true }).click();
      const answer = await created;
      expect(answer.status()).toBe(201);
      serviceId = (await answer.json()).service_id;

      // The engine page opens with the new engine and its key stored.
      await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`engine=${serviceId}`));
      await expect(page.getByText("Configured", { exact: true })).toBeVisible();
      // The Models section answers the same question for the saved models.
      await expect(page.getByRole("heading", { name: "Who can use these models" })).toBeVisible();
      await expect(page.getByText(/^Usable by:/)).toBeVisible();
      await expect(page.getByRole("link", { name: "Access plans" })).toBeVisible();

      // N2: an edited address never gets the saved key, and the page says so before Save.
      await page.getByLabel(/^Address/).fill(`http://127.0.0.1:${enginePort + 5}/v1`);
      await expect(page.getByText("Your saved key won’t be sent to a new address. Re-enter it, or keep the old address.")).toBeVisible();
      await expect(page.getByText("Key not sent to this address yet. Re-enter it to check.")).toBeVisible();
      await expect(page.getByText(/Saving removes the old key from this engine\./)).toBeVisible();
      await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
      await page.getByRole("button", { name: "Keep the old address" }).click();
      await expect(page.getByText("Your saved key won’t be sent to a new address.", { exact: false })).toHaveCount(0);
      await expect(page.getByText("Configured", { exact: true })).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    } finally {
      if (serviceId) {
        const session = await (await page.request.get("/admin/api/session")).json();
        const registry = await page.request.get("/admin/api/operator/service-registry");
        const removed = await page.request.delete(`/admin/api/operator/services/${serviceId}`, {
          headers: { "x-azizam-csrf": session.actor.csrf_token, "if-match": registry.headers().etag },
        });
        expect(removed.ok()).toBe(true);
      }
    }
  });
}
