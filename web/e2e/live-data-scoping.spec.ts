import { expect, request, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { taskNames } from "../src/lib/terms";

type Manifest = {
  service_asset: string;
  local_asset: string;
  member_asset: string;
  owner_asset: string;
  owner_audio: string;
  owner_service_asset: string;
  service_app_id: string;
  member_app_id: string;
};

async function manifest(): Promise<Manifest> {
  return JSON.parse(
    await readFile("test-results/live-data-manifest.json", "utf8"),
  ) as Manifest;
}

test("owner sees their own media and usage, not a member's work in a service App; keyless requests are refused", async ({
  page,
  baseURL,
}) => {
  const data = await manifest();

  // There is no keyless access (ACCESS_PLANS.md §2.8).
  const anon = await request.newContext({ baseURL });
  const keyless = await anon.post("/v1/chat/completions", {
    headers: { "user-agent": "Open WebUI live-data e2e" },
    data: {
      model: "fast",
      messages: [{ role: "user", content: "keyless LAN source check" }],
    },
  });
  expect(keyless.status()).toBe(401);
  expect((await keyless.json()).error.code).toBe("key_required");
  await anon.dispose();

  // What an owner reads follows who made it (OWNER_VISIBILITY). Owning Daisy grants nothing in it: the member's
  // work in Daisy is theirs alone, and the keyless LAN clip has no maker, so no one reads it.
  const media = await (await page.request.get("/me/api/media")).json();
  const mediaSources = media.sources.map((source: { id: string }) => source.id);
  expect(mediaSources).toEqual(
    expect.arrayContaining([`app:${data.service_app_id}`]),
  );
  expect(mediaSources).not.toContain("local-network");
  expect(mediaSources).not.toContain(`app:${data.member_app_id}`);
  const ids = media.assets.map((asset: { asset_id: string }) => asset.asset_id);
  expect(ids).toEqual(
    expect.arrayContaining([data.owner_asset, data.owner_audio, data.owner_service_asset]),
  );
  expect(ids).not.toContain(data.member_asset);
  expect(ids).not.toContain(data.service_asset);
  expect(ids).not.toContain(data.local_asset);

  const serviceMedia = await (
    await page.request.get(`/me/api/media?source=app:${data.service_app_id}`)
  ).json();
  expect(
    serviceMedia.assets.map((asset: { asset_id: string }) => asset.asset_id),
  ).toEqual([data.owner_service_asset]);
  expect(
    (await page.request.get("/me/api/media?source=local-network")).status(),
  ).toBe(400);

  const usage = await (
    await page.request.get("/me/api/usage/summary?person=all")
  ).json();
  expect(usage.requests).toBeGreaterThanOrEqual(1);
  expect(usage.media_count).toBeGreaterThanOrEqual(1);
  expect(
    usage.sources.map((source: { id: string }) => source.id),
  ).not.toContain("local-network");
  expect(
    (await page.request.get("/me/api/usage/summary?person=all&source=local-network")).status(),
  ).toBe(400);

  await page.goto("/library/");
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  await expect(page.getByText("No media yet")).toHaveCount(0);
  // The owner's Library has no person choice; its three items are the owner's own work.
  await expect(page.getByRole("combobox", { name: "Person", exact: true })).toHaveCount(0);
  await expect(page.locator("article")).toHaveCount(3);
  await expect(page.getByText("Library could not be loaded")).toHaveCount(0);
  await page.goto("/usage/");
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  // The seeded audio.speech request, under its task name from lib/terms
  // (taskNames.speech), then its model after a "·" (one line since craft
  // PR 6) and its count.
  await expect(
    page.getByRole("row", { name: new RegExp(`${taskNames.speech} (· )?Voice 1\\b`) }),
  ).toBeVisible();
  // The same row by its literal copy, the task name Home and Try use.
  await expect(
    page.getByRole("row", { name: /Read text aloud (· )?Voice 1\b/ }),
  ).toBeVisible();
});

test("member sees their own media, and cannot reach the owner's work in a service App", async ({
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
    ownMedia.assets.map((asset: { asset_id: string }) => asset.asset_id).sort(),
  ).toEqual([data.member_asset, data.service_asset].sort());
  // Sources are the Apps the member runs; Daisy's media is theirs without being an App of theirs.
  expect(ownMedia.sources.map((source: { id: string }) => source.id)).toEqual([
    `app:${data.member_app_id}`,
  ]);

  expect(
    (await member.get("/me/api/media?source=local-network")).status(),
  ).toBe(400);
  // Filtering by Daisy stays refused to a member; their own picture there is in the unfiltered list above.
  expect(
    (await member.get(`/me/api/media?source=app:${data.service_app_id}`)).status(),
  ).toBe(403);
  expect(
    (await member.get(`/me/api/media/${data.owner_service_asset}/download`)).status(),
  ).toBe(404);
  expect(
    (await member.get(`/me/api/media/${data.owner_asset}/download`)).status(),
  ).toBe(404);
  expect(
    (await member.get("/me/api/usage/summary?source=local-network")).status(),
  ).toBe(400);

  const page = await context.newPage();
  await page.goto("/library/");
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  await expect(page.getByText("No media yet")).toHaveCount(0);
  await expect(page.locator("article")).toHaveCount(2);
  await context.close();
});
