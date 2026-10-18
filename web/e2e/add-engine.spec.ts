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
      await expect(dialog.getByText("Already added", { exact: true }).first()).toBeVisible();
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

      // The record page exposes all six contract sections. A real save is
      // followed by a reload so local form state cannot make this pass.
      for (const section of ["Connection", "What it offers", "How it runs", "What Azizam can check", "Resources", "Stored record"]) {
        await expect(page.getByRole("heading", { name: section, exact: true })).toBeVisible();
      }
      await expect(page.getByTestId("connection-facts")).toContainText(`${address}/chat/completions`);
      const storedTest = page.waitForResponse((response) =>
        response.request().method() === "PATCH" &&
        response.url().endsWith(`/admin/api/operator/services/${serviceId}`) &&
        Boolean(response.request().postDataJSON()?.tests),
      );
      await page.getByRole("button", { name: "Run a real test for Chat" }).click();
      const realTest = page.getByRole("status", { name: "Real test of Chat" });
      await expect(realTest).toBeVisible();
      await realTest.getByText("Exact request sent", { exact: true }).click();
      const trace = realTest.getByLabel("Exact request sent");
      await expect(trace).toContainText(`POST ${address}/chat/completions`);
      await expect(trace).toContainText("Bearer [redacted]");
      await expect(trace).not.toContainText("sk-journey-local-key");
      await expect(trace).toContainText('"messages"');
      // The successful real test records its result in a separate registry write.
      // Complete that write before this journey edits the same record.
      expect((await storedTest).status()).toBe(200);
      await page.getByLabel(/^Name/).fill(`${name} edited`);
      const saved = page.waitForResponse(
        (response) => response.request().method() === "PATCH" && response.url().endsWith(`/admin/api/operator/services/${serviceId}`),
      );
      await page.getByRole("button", { name: "Save changes" }).click();
      expect((await saved).status()).toBe(200);
      await page.reload();
      await expect(page.getByRole("heading", { level: 1, name: `${name} edited` })).toBeVisible();
      await expect(page.getByTestId("connection-facts")).toContainText(`${address}/chat/completions`);

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

test("a header-only save keeps an absent reply timeout and hidden header rows", { tag: GLOBAL_STATE_TAG }, async ({ page, baseURL }) => {
  const session = await (await page.request.get("/admin/api/session")).json();
  const registry = async () => {
    const res = await page.request.get("/admin/api/operator/service-registry");
    return { value: (await res.json()).service_registry, etag: res.headers().etag };
  };
  const headers = (etag: string) => ({ "x-azizam-csrf": session.actor.csrf_token, "if-match": etag });
  let serviceId = "";
  try {
    const created = await page.request.post("/admin/api/operator/services", {
      headers: headers((await registry()).etag),
      data: { definition: "openai-compatible", name: "Header preservation journey", disabled: true,
        values: { address: `http://127.0.0.1:${Number(new URL(baseURL!).port) + 1}/provider/v1` } },
    });
    expect(created.status()).toBe(201);
    serviceId = (await created.json()).service_id;
    const seed = await registry();
    const config = seed.value.services[serviceId].config;
    delete config.timeout;
    config.gateway_lifecycle = true;
    config.headers = [{ name: "X-Api-Key", value: "test-only-hidden-header" }, { name: "X-Private", value: "test-only-private-header", secret: true }];
    expect((await page.request.put("/admin/api/operator/service-registry", { headers: headers(seed.etag), data: seed.value })).ok()).toBe(true);
    await page.goto(`/engines/?engine=${serviceId}`);
    await expect(page.getByRole("heading", { level: 1, name: "Header preservation journey" })).toBeVisible();
    await expect(page.getByTestId("connection-facts")).toContainText("default for engines Azizam starts");
    const connection = page.getByRole("region", { name: "More options", exact: true });
    await expect(connection.getByText("Not sent", { exact: true })).toHaveCount(2);
    await connection.getByRole("button", { name: "Add a header" }).click();
    await connection.getByLabel("Header name", { exact: true }).last().fill("X-Tenant");
    await connection.getByLabel("Value", { exact: true }).last().fill("household");
    const saved = page.waitForResponse(res => res.request().method() === "PATCH" && res.url().endsWith(`/services/${serviceId}`));
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    expect((await saved).status()).toBe(200);
    const after = (await registry()).value.services[serviceId].config;
    expect(after).not.toHaveProperty("timeout");
    expect(after.headers).toHaveLength(3);
    expect(after.headers).toContainEqual({ name: "X-Tenant", value: "household" });
    await page.reload();
    await expect(page.getByTestId("connection-facts")).toContainText("default for engines Azizam starts");
    await expect(connection.getByText("Not sent", { exact: true })).toHaveCount(2);
    await connection.getByRole("button", { name: "Remove header X-Api-Key" }).click();
    const removed = page.waitForResponse(res => res.request().method() === "PATCH" && res.url().endsWith(`/services/${serviceId}`));
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    expect((await removed).status()).toBe(200);
    const final = (await registry()).value.services[serviceId].config;
    expect(final.headers.map((row: { name: string }) => row.name)).toEqual(["X-Private", "X-Tenant"]);
    expect(final).not.toHaveProperty("timeout");
  } finally {
    if (serviceId) await page.request.delete(`/admin/api/operator/services/${serviceId}`, { headers: headers((await registry()).etag) });
  }
});
