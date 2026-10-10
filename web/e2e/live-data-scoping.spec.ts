import { expect, request, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { taskNames } from "../src/lib/terms";

type Manifest = {
  service_asset: string;
  local_asset: string;
  member_asset: string;
  owner_asset: string;
  service_app_id: string;
  member_app_id: string;
};

async function manifest(): Promise<Manifest> {
  return JSON.parse(
    await readFile("test-results/live-data-manifest.json", "utf8"),
  ) as Manifest;
}

test("owner sees keyless LAN and service app media and usage", async ({
  page,
  baseURL,
}) => {
  const data = await manifest();

  const anon = await request.newContext({ baseURL });
  const keyless = await anon.post("/v1/chat/completions", {
    headers: { "user-agent": "Open WebUI live-data e2e" },
    data: {
      model: "fast",
      messages: [{ role: "user", content: "keyless LAN source check" }],
    },
  });
  expect(keyless.status()).toBe(200);
  await anon.dispose();

  const media = await (await page.request.get("/me/api/media")).json();
  expect(media.sources.map((source: { id: string }) => source.id)).toEqual(
    expect.arrayContaining(["local-network", `app:${data.service_app_id}`]),
  );
  expect(
    media.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual(
    expect.arrayContaining([
      data.service_asset,
      data.owner_asset,
      data.local_asset,
    ]),
  );

  const serviceMedia = await (
    await page.request.get(`/me/api/media?source=app:${data.service_app_id}`)
  ).json();
  expect(
    serviceMedia.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual([data.service_asset]);
  const localMedia = await (
    await page.request.get("/me/api/media?source=local-network")
  ).json();
  expect(
    localMedia.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual(expect.arrayContaining([data.local_asset]));

  const usage = await (
    await page.request.get("/me/api/usage/summary?source=local-network")
  ).json();
  expect(usage.requests).toBeGreaterThanOrEqual(2);
  expect(usage.media_count).toBeGreaterThanOrEqual(1);
  expect(
    usage.receipts.some(
      (receipt: { source?: { kind?: string } }) =>
        receipt.source?.kind === "local-network",
    ),
  ).toBe(true);

  await page.goto("/ui/library/");
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  await expect(page.getByText("No media yet")).toHaveCount(0);
  // Model, date and app live in the "Filters" popover since D1.
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("combobox", { name: "Source", exact: true }).click();
  await page.getByRole("option", { name: "Daisy", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.locator("article")).toHaveCount(1);
  await page.getByRole("button", { name: /^Open Picture, / }).click();
  await page.getByText("Details", { exact: true }).click();
  await expect(
    page.getByRole("dialog").getByText("Daisy", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  // The owner filter is "Person"; the viewer's own option is "<name> (you)".
  await page.getByRole("combobox", { name: "Person", exact: true }).click();
  await page.getByRole("option", { name: /\(you\)$|^You$/ }).click();
  await expect(page.locator("article")).toHaveCount(1);
  const mine = await (await page.request.get("/me/api/media?scope=me")).json();
  expect(
    mine.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual([data.owner_asset]);
  await expect(page.getByText("No media yet")).toHaveCount(0);
  await expect(page.getByText("Library could not be loaded")).toHaveCount(0);
  await page.goto("/ui/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  // The seeded audio.speech request, under its task name from lib/terms
  // (taskNames.speech), then its model after a "·" (one line since craft
  // PR 6) and its count.
  await expect(
    page.getByRole("row", { name: new RegExp(`${taskNames.speech} · Voice 1\\b`) }),
  ).toBeVisible();
  // The same row by its literal copy, the task name Home and Try use.
  await expect(
    page.getByRole("row", { name: /Read text aloud · Voice 1\b/ }),
  ).toBeVisible();
});

test("member cannot see local/service media or mint signed URLs for them", async ({
  browser,
  baseURL,
}) => {
  const data = await manifest();
  const context = await browser.newContext({
    baseURL,
    storageState: "test-results/live-data-member.json",
  });
  const member = context.request;

  const ownMedia = await (await member.get("/me/api/media")).json();
  expect(
    ownMedia.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual([data.member_asset]);
  expect(ownMedia.sources.map((source: { id: string }) => source.id)).toEqual([
    `app:${data.member_app_id}`,
  ]);

  expect(
    (await member.get("/me/api/media?source=local-network")).status(),
  ).toBe(403);
  expect(
    (
      await member.get(`/me/api/media?source=app:${data.service_app_id}`)
    ).status(),
  ).toBe(403);
  expect(
    (await member.get(`/me/api/media/${data.service_asset}/download`)).status(),
  ).toBe(404);
  expect(
    (await member.get("/me/api/usage/summary?source=local-network")).status(),
  ).toBe(403);

  const page = await context.newPage();
  await page.goto("/ui/library/");
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  await expect(page.getByText("No media yet")).toHaveCount(0);
  await context.close();
});
