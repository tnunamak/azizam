import { expect, test, type Page } from "@playwright/test";

// A llama.cpp engine reports the GGUF file it loaded, so a model id can be a
// directory on the gateway host. No screen shows it outside an owner's
// technical details.
const GGUF =
  "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";
const HOST_PATH = /\/media\/windows|AI Models|\/GGUF\/|\/home\/[a-z]+\//;

async function visibleText(page: Page) {
  return page.evaluate(() => document.body.innerText);
}

async function expectNoHostPath(page: Page, where: string) {
  // Titles and aria labels are on-demand text too.
  const attributes = await page.evaluate(() =>
    [...document.querySelectorAll("[title],[aria-label]")]
      .map((node) => `${node.getAttribute("title") ?? ""} ${node.getAttribute("aria-label") ?? ""}`)
      .join("\n"),
  );
  expect(await visibleText(page), `${where}: visible text`).not.toMatch(HOST_PATH);
  expect(attributes, `${where}: title and aria-label`).not.toMatch(HOST_PATH);
}

test("Usage shows no host path in the table, receipts or details", async ({ page }) => {
  await page.route("**/me/api/usage/summary?**", (route) =>
    route.fulfill({
      json: {
        requests: 2,
        by_model: [{ model: GGUF, capability: "chat", requests: 2 }],
        by_day: [{ date: "2026-10-01", chat: 2 }],
        receipts: [
          { request_id: "r1", capability: "chat", requested_model: "default", resolved_model: GGUF, status: "succeeded", accepted_at_ms: 1790870400000 },
        ],
      },
    }),
  );
  await page.goto("/ui/usage/");
  await expect(page.getByText("Top models by requests")).toBeVisible();
  await page.getByRole("button", { name: "View recent requests" }).click();
  await expectNoHostPath(page, "usage");
  await page.getByRole("button", { name: /Receipt details for Chat request/ }).click();
  await expectNoHostPath(page, "usage receipt");
  // The owner's technical details keep the exact id for routing diagnostics.
  // Everything outside that disclosure stays free of the path.
  await page.getByText("Technical details", { exact: true }).click();
  const outside = await page.evaluate(() => {
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("details").forEach((node) => node.remove());
    return clone.innerText ?? clone.textContent ?? "";
  });
  expect(outside, "usage receipt: outside technical details").not.toMatch(HOST_PATH);
  await expect(page.locator("details", { hasText: "Technical details" })).toContainText(
    "/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf",
  );
});

test("Activity shows no host path in rows or details", async ({ page }) => {
  await page.route("**/admin/api/activity/requests?*", (route) =>
    route.fulfill({
      json: {
        records: [
          {
            receipt: { request_id: "req_path", intent: "chat.completions", requested_model: "default", resolved_model: GGUF, status: "succeeded", latency_ms: 120 },
            person_label: "Owner",
            app_label: "Try",
            created_at: Math.floor(Date.now() / 1000),
          },
        ],
        next_cursor: null,
        people: [],
        sources: [],
      },
    }),
  );
  await page.goto("/ui/activity/");
  await page.getByRole("tab", { name: "Requests", exact: true }).click();
  const row = page.getByRole("button", { name: /Request details:/ }).first();
  await expect(row).toBeVisible();
  await expectNoHostPath(page, "activity");
  await row.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectNoHostPath(page, "activity details");
});

test("Models shows no host path when a model is registered by file path", async ({ page }) => {
  await page.route("**/me/api/models/catalog", (route) =>
    route.fulfill({
      json: {
        schema_version: 1,
        object: "models.catalog",
        aliases: [],
        models: [
          {
            id: GGUF,
            object: "model_catalog.model",
            display_name: GGUF,
            name: GGUF,
            alias: false,
            operations: ["chat.completions"],
            resolves_to: {},
            description: null,
            capabilities: [],
            provider: null,
            engine_label: "llama.cpp",
            locality: "local",
            cost: { kind: "free", price: null },
            timing: {},
            availability: { available: true, status: "ready" },
            io: { input_modalities: ["text"], output_modalities: ["text"] },
            examples: [],
            recent_receipts: [],
            use: { model: GGUF },
            type: "language",
          },
        ],
        counts: { aliases: 0, models: 1, capabilities: 0 },
      },
    }),
  );
  await page.goto("/ui/models/");
  await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();
  const all = page.getByRole("button", { name: "Show all models" });
  if (await all.count()) await all.click();
  await expect(page.getByText(/Qwen3\.8/).first()).toBeVisible();
  await expectNoHostPath(page, "models");
});

test("Library shows no host path in a creation's details", async ({ page }) => {
  await page.route("**/me/api/media?**", (route) =>
    route.fulfill({
      json: {
        assets: [
          { asset_id: "asset_1", kind: "image", prompt: "A lighthouse", model: GGUF, created_at: 1_760_000_000, download_url: "/me/api/media/asset_1/download" },
        ],
        models: [GGUF],
        next_cursor: null,
      },
    }),
  );
  await page.goto("/ui/library/");
  await page.getByRole("button", { name: /Open A lighthouse/ }).click();
  await expectNoHostPath(page, "library");
});
