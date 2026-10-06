import { test, expect } from "@playwright/test";

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

test("real registry journey preserves disabled state and unknown config through endpoint edits", async ({
  page,
  baseURL,
}) => {
  const enginePort = Number(new URL(baseURL!).port) + 1;
  await page.goto("/ui/engines/");
  await expect(
    page.getByRole("heading", { name: "Engines", level: 1 }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Add engine", exact: true })
    .first()
    .click();
  const add = page.getByRole("dialog", { name: "Add engine", exact: true });
  await add.getByRole("combobox", { name: "Preset" }).click();
  await page.getByRole("option", { name: /OpenAI.?compatible/i }).click();
  await add.getByLabel(/^Name/).fill("Journey engine");
  await add.getByLabel(/^Base URL/).fill(`http://127.0.0.1:${enginePort}/v1`);
  await add.getByRole("button", { name: "Add engine", exact: true }).click();
  await expect(add).not.toBeVisible();

  const registry = async (): Promise<RegistryResponse> => {
    const response = await page.request.get(
      "/admin/api/operator/service-registry",
    );
    expect(response.ok()).toBe(true);
    return {
      ...(await response.json()).service_registry,
      etag: response.headers().etag,
    };
  };
  const initial = await registry();
  const record = Object.values(initial.services).find(
    (row) => row.name === "Journey engine",
  );
  expect(record).toBeTruthy();
  if (!record) throw new Error("Journey engine was not created");
  expect(initial.etag).toBeTruthy();
  if (!initial.etag)
    throw new Error("Registry response did not include an ETag");
  const session = await (await page.request.get("/admin/api/session")).json();
  const seeded = await page.request.patch(
    `/admin/api/operator/services/${record.id}`,
    {
      headers: {
        "x-azizam-csrf": session.actor.csrf_token,
        "x-azizam-admin-action": "1",
        "if-match": initial.etag,
      },
      data: {
        health_endpoint: "/health",
        custom_operator_field: { preserve: true },
      },
    },
  );
  expect(seeded.ok()).toBe(true);

  const withLegacyModels = await registry();
  expect(withLegacyModels.etag).toBeTruthy();
  if (!withLegacyModels.etag) {
    throw new Error("Registry response did not include an ETag after seeding");
  }
  withLegacyModels.services[record.id].config.models = {
    "legacy-chat": {
      upstream_model: "legacy-upstream",
      routing_hint: "keep-me",
    },
    slot: {
      model_group: "public",
      deployment: "real",
      routing_hint: "keep-slot",
    },
  };
  const { etag: legacyEtag, ...legacyRegistry } = withLegacyModels;
  const legacySeeded = await page.request.put(
    "/admin/api/operator/service-registry",
    {
      headers: {
        "x-azizam-csrf": session.actor.csrf_token,
        "x-azizam-admin-action": "1",
        "if-match": legacyEtag,
      },
      data: legacyRegistry,
    },
  );
  expect(legacySeeded.ok()).toBe(true);
  await page.reload();
  let card = page.locator(`article[data-engine-id="${record.id}"]`);
  await page.getByRole("switch", { name: "Show not in use" }).click();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Test engine", exact: true })
    .click();
  let sheet = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await sheet.getByRole("button", { name: "Test", exact: true }).click();
  await expect(sheet.getByText(/Connected to/).first()).toBeVisible();
  await page.keyboard.press("Escape");
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Detect models", exact: true })
    .click();
  sheet = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await sheet.getByRole("button", { name: "Detect", exact: true }).click();
  await expect(sheet.getByText(/Detected models:/).first()).toBeVisible();
  await page.keyboard.press("Escape");
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Edit engine", exact: true })
    .click();
  sheet = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await sheet.getByRole("switch", { name: /Toggle / }).click();
  await page.keyboard.press("Escape");
  await expect(card).toBeVisible();
  await expect(card.getByText("Disabled", { exact: true })).toBeVisible();
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Edit engine", exact: true })
    .click();
  const edit = page.getByRole("dialog", { name: "Edit engine", exact: true });
  await edit.getByLabel(/^Name/).fill("Renamed journey engine");
  await edit
    .getByLabel("Endpoint", { exact: true })
    .fill(`http://127.0.0.1:${enginePort}/v2`);
  await edit.getByRole("button", { name: "Save engine", exact: true }).click();
  await expect(edit).not.toBeVisible();
  const after = (await registry()).services[record.id];
  expect(after.name).toBe("Renamed journey engine");
  expect(after.enabled).toBe(false);
  expect(after.config.host).toBe("127.0.0.1");
  expect(after.config.port).toBe(enginePort);
  expect(after.config.base_path).toBe("/v2");
  expect(after.config.custom_operator_field).toEqual({ preserve: true });
  expect(after.config.models).toEqual([
    expect.objectContaining({
      id: "legacy-chat",
      model_group: "legacy-chat",
      upstream_model: "legacy-upstream",
      routing_hint: "keep-me",
    }),
    expect.objectContaining({
      id: "slot",
      model_group: "public",
      upstream_model: "real",
      deployment: "real",
      routing_hint: "keep-slot",
    }),
  ]);
  card = page.locator(`article[data-engine-id="${record.id}"]`);
  await card.getByRole("button", { name: /Actions|More/ }).click();
  await page
    .getByRole("menuitem", { name: "Remove engine", exact: true })
    .click();
  const remove = page.getByRole("dialog", {
    name: "Remove engine?",
    exact: true,
  });
  const typed = remove.getByRole("textbox");
  if (await typed.count()) await typed.fill("Renamed journey engine");
  await remove
    .getByRole("button", { name: "Remove engine", exact: true })
    .click();
  await expect(remove).not.toBeVisible();
  expect((await registry()).services[record.id]).toBeUndefined();
});
