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

  // Unclassified keys are never shown (D34): Technical details only says one is kept, and the key stays off the page.
  await page.getByText("Technical details", { exact: true }).click();
  await expect(page.getByText("1 setting is kept but not shown", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Other settings" })).toHaveCount(0);
  await expect(page.getByText("custom_operator_field")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: /JSON/ })).toHaveCount(0);

  await page.getByLabel(/^Name/).fill("Renamed journey engine");
  await page.getByLabel(/^Address/).fill(`http://127.0.0.1:${enginePort}/v2`);
  const more = page.locator("details").filter({ has: page.getByText("More options", { exact: true }) }).first();
  await more.getByText("More options", { exact: true }).click();
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
  // The projection leaks no unclassified key, yet the engine view still counts the one stored key: kept through the edit.
  expect(after.config.custom_operator_field).toBeUndefined();
  const afterView = (await (await page.request.get(`/admin/api/engines/${serviceId}`)).json()).engine;
  expect(afterView.hidden_settings).toBe(1);
  expect(JSON.stringify(afterView)).not.toContain("custom_operator_field");
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
