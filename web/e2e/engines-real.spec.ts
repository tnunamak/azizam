import { expect, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

type RegistryService = {
  id: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
};

type RegistryResponse = {
  services: Record<string, RegistryService>;
  etag: string | undefined;
  [key: string]: unknown;
};

// The engine page keeps what it does not show: an engine kept off stays off,
// other settings and older model rows survive an address edit, and a request
// adjustment is added with its typed form, never as JSON.
test("real registry journey preserves disabled state and unknown config through address edits", { tag: GLOBAL_STATE_TAG }, async ({
  page,
  baseURL,
}) => {
  const enginePort = Number(new URL(baseURL!).port) + 1;
  const registry = async (): Promise<RegistryResponse> => {
    const response = await page.request.get("/admin/api/operator/service-registry");
    expect(response.ok()).toBe(true);
    return { ...(await response.json()).service_registry, etag: response.headers().etag };
  };
  const session = await (await page.request.get("/admin/api/session")).json();
  const writeHeaders = (etag: string | undefined) => ({
    "x-azizam-csrf": session.actor.csrf_token,
    "x-azizam-admin-action": "1",
    "if-match": etag ?? "",
  });

  // Add it kept off through the same route the dialog uses.
  const created = await page.request.post("/admin/api/operator/services", {
    headers: writeHeaders((await registry()).etag),
    data: {
      definition: "openai-compatible",
      name: "Journey engine",
      // The seed already serves /v1 on this port; a duplicate address stops.
      values: { address: `http://127.0.0.1:${enginePort}/provider/v1` },
      disabled: true,
    },
  });
  expect(created.status()).toBe(201);
  const serviceId: string = (await created.json()).service_id;

  // Older data the form has no field for: an unknown key and models kept as a mapping.
  const seeded = await registry();
  seeded.services[serviceId].config = {
    ...seeded.services[serviceId].config,
    custom_operator_field: { preserve: true },
    models: {
      "existing-chat": { upstream_model: "existing-upstream", routing_hint: "keep-me" },
      slot: { model_group: "public", deployment: "real", routing_hint: "keep-slot" },
    },
  };
  const { etag, ...body } = seeded;
  const put = await page.request.put("/admin/api/operator/service-registry", {
    headers: writeHeaders(etag),
    data: body,
  });
  expect(put.ok()).toBe(true);

  await page.goto("/engines/");
  await expect(page.getByRole("heading", { name: "Engines", level: 1 })).toBeVisible();
  const card = page.locator(`tr[data-engine-id="${serviceId}"]`);
  await page.getByRole("switch", { name: "Show not in use" }).click();
  await expect(card).toBeVisible();
  // P5: Azizam does not start this engine itself, so it says when the check runs.
  await expect(card.getByText("Not checked yet; checked when you turn it on or press Check now")).toBeVisible();
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page.getByRole("menuitem", { name: "Open engine", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Journey engine" })).toBeVisible();

  // Other settings remain read-only under the stored record.
  await page.getByRole("heading", { name: "Stored record", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Other settings" })).toBeVisible();
  await expect(page.getByText("custom_operator_field", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /JSON/ })).toHaveCount(0);

  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Renamed journey engine");
  await page.getByLabel(/^Address/).fill(`http://127.0.0.1:${enginePort}/v2`);
  const more = page.getByRole("region", { name: "What it offers", exact: true });
  await more.getByText("Add an adjustment").click();
  await more.getByRole("button", { name: /Set defaults/ }).click();
  await more.getByLabel("Field (row 1)").fill("max_tokens");
  await more.getByLabel("Value when missing (row 1)").fill("9");
  await expect(more.getByText(/Azizam adds “max_tokens” = “9”/)).toBeVisible();

  const saved = page.waitForResponse(
    (response) => response.request().method() === "PATCH" && response.url().endsWith(`/services/${serviceId}`),
  );
  await page.getByRole("button", { name: "Save and keep off" }).click();
  expect((await saved).status()).toBe(200);
  const after = (await registry()).services[serviceId];
  expect(after.name).toBe("Renamed journey engine");
  expect(after.enabled).toBe(false);
  expect(after.config.host).toBe("127.0.0.1");
  expect(after.config.port).toBe(enginePort);
  expect(after.config.base_path).toBe("/v2");
  expect(after.config.custom_operator_field).toEqual({ preserve: true });
  expect(after.config.transforms).toContainEqual({ kind: "default-params", set: { max_tokens: 9 } });
  expect(after.config.models).toEqual([
    expect.objectContaining({ model_group: "existing-chat", upstream_model: "existing-upstream", routing_hint: "keep-me" }),
    expect.objectContaining({ model_group: "public", upstream_model: "real", routing_hint: "keep-slot" }),
  ]);

  // Remove it from the list: typed confirmation, then focus moves to a neighbour.
  await page.goto("/engines/");
  await page.getByRole("switch", { name: "Show not in use" }).click();
  await expect(card).toBeVisible();
  const actionButtons = page.locator("tr[data-engine-id] button[aria-haspopup='menu']:visible");
  const rowIds = await actionButtons.evaluateAll((buttons) =>
    buttons.map((button) => button.closest("[data-engine-id]")!.getAttribute("data-engine-id")),
  );
  const removedIndex = rowIds.indexOf(serviceId);
  expect(removedIndex).toBeGreaterThanOrEqual(0);
  const neighbourId = rowIds[removedIndex + 1] ?? rowIds[removedIndex - 1];
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page.getByRole("menuitem", { name: "Remove engine", exact: true }).click();
  const remove = page.getByRole("dialog", { name: "Remove engine?", exact: true });
  const typed = remove.getByRole("textbox");
  const confirm = remove.getByRole("button", { name: "Remove engine", exact: true });
  await expect(confirm).toBeDisabled();
  await typed.fill("wrong name");
  await expect(confirm).toBeDisabled();
  await typed.fill("Renamed journey engine");
  await confirm.click();
  await expect(remove).not.toBeVisible();
  await expect(card).toHaveCount(0);
  const focusTarget = neighbourId
    ? page.locator(`tr[data-engine-id="${neighbourId}"] button[aria-haspopup='menu']`)
    : page.getByRole("button", { name: "Add engine", exact: true }).first();
  await expect(focusTarget).toBeFocused();
  expect((await registry()).services[serviceId]).toBeUndefined();
});

for (const width of [1280, 390]) {
  test(`engine contract edits preserve arguments and fit at ${width}px`, { tag: GLOBAL_STATE_TAG }, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const read = async () => {
      const response = await page.request.get("/admin/api/operator/service-registry");
      expect(response.ok()).toBe(true);
      return { value: (await response.json()).service_registry, revision: response.headers().etag };
    };
    const session = await (await page.request.get("/admin/api/session")).json();
    const headers = (revision?: string) => ({
      "x-azizam-csrf": session.actor.csrf_token, "x-azizam-admin-action": "1", "if-match": revision ?? "",
    });
    const created = await page.request.post("/admin/api/operator/services", {
      headers: headers((await read()).revision), data: {
        definition: "openai-compatible", name: `Contract ${width}`,
        values: { address: `http://127.0.0.1:${Number(new URL(baseURL!).port) + 1}/contract-${width}/v1` }, disabled: true,
      },
    });
    expect(created.status()).toBe(201);
    const id = (await created.json()).service_id;
    const seed = await read();
    const record = seed.value.services[id];
    const start = ["runner", "--model", "/models/family, chat.bin", "--token", "isolated-command-key"];
    const stop = ["stop-runner", "family chat"];
    record.lifecycle = { driver: "command", driver_config: { start, stop, health_timeout_s: 120 } };
    record.process = { driver: "command", start, stop, legacy_lifecycle: record.lifecycle };
    record.config.engine_capabilities = ["chat"];
    const longId = "family/" + "large-model-name-".repeat(16);
    record.config.models = [{ model_group: "family-chat", upstream_model: longId }];
    record.config.transforms = [{ kind: "param-map", from: "old_field", to: "new_field" }];
    const put = await page.request.put("/admin/api/operator/service-registry", { headers: headers(seed.revision), data: seed.value });
    expect(put.ok()).toBe(true);
    await page.goto(`/engines/?engine=${id}`);
    await expect(page.getByRole("heading", { level: 1, name: `Contract ${width}` })).toBeVisible();
    await expect(page.getByTestId("engine-health")).toContainText("Unknown");
    const arg = page.getByRole("textbox", { name: "Start command argument 3" });
    await expect(arg).toHaveValue(start[2]);
    await expect(page.getByRole("textbox", { name: "Start command argument 5" })).toHaveValue("[redacted]");
    await arg.fill("/models/new family, chat.bin");
    const from = page.getByLabel("Field name (row 1)");
    const to = page.getByLabel("Rename to (row 1)");
    await from.scrollIntoViewIfNeeded();
    await expect(from).toBeVisible();
    await expect(to).toBeVisible();
    if (width === 390) {
      const first = await from.boundingBox();
      const second = await to.boundingBox();
      expect(second!.y).toBeGreaterThan(first!.y + first!.height);
      expect(first!.width).toBeGreaterThan(200);
      await page.screenshot({ path: "test-results/engine-contract-390-inputs.png" });
      const saveBar = page.getByRole("status").filter({ hasText: "You have unsaved changes." });
      await expect(saveBar).toHaveCSS("position", "static");
    }
    const saved = page.waitForResponse(r => r.request().method() === "PATCH" && r.url().endsWith(`/services/${id}`));
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    expect((await saved).status()).toBe(200);
    const after = (await read()).value.services[id];
    expect(after.process.start).toEqual([start[0], start[1], "/models/new family, chat.bin", "--token", "[redacted]"]);
    expect(after.process.stop).toEqual(stop);
    await page.reload();
    await expect(page.getByRole("textbox", { name: "Start command argument 3" })).toHaveValue("/models/new family, chat.bin");
    // Long workflow IDs and required inputs stay inspectable even without an editor.
    const workflow = await read();
    workflow.value.services[id].offerings = [{ id: longId, kind: "workflow", tasks: [{ task: "images", evidence: "declared" }], inputs: { prompt: { type: "string", required: true } }, options: { steps: { type: "integer" } }, selection: { mode: "whole_graph" } }];
    expect((await page.request.put("/admin/api/operator/service-registry", { headers: headers(workflow.revision), data: workflow.value })).ok()).toBe(true);
    await page.reload();
    await expect(page.getByTestId("offering-facts")).toContainText("prompt");
    await page.getByTestId("offering-facts").scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/engine-contract-${width}.png` });
  });
}
