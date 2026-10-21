import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

// "Choose from Library" on Try's media inputs, as a person whose Library holds
// what their other apps made (the household's Robin), against the real gateway
// and a fake engine that records every file the gateway sends it. A pick must
// show the real picture in the dialog and in the form, and the engine must
// receive that picture's own bytes. The Library's download route answers with
// a signed link, not the file, and the Try key only owns what Try made.

type Manifest = { member_email: string; password: string };
type LibraryItem = {
  asset_id: string;
  prompt?: string | null;
  size_bytes: number;
  download_url: string;
};
type Upload = { filename: string; bytes: number; head_hex: string; sha256: string };

let seed: Manifest;
let signedIn: Awaited<ReturnType<BrowserContext["storageState"]>>;
let enginePort: number;

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ baseURL, browser }) => {
  const port = Number(new URL(baseURL!).port);
  enginePort = port + 1;
  seed = JSON.parse(
    await readFile(
      path.resolve(__dirname, `../test-results/household-manifest-${port}.json`),
      "utf8",
    ),
  );
  // Sign-in is throttled: sign in once and share the session.
  const ctx = await browser.newContext({
    baseURL,
    storageState: { cookies: [], origins: [] },
  });
  const page = await ctx.newPage();
  await page.goto("/sign-in/");
  await page.getByLabel("Email", { exact: true }).fill(seed.member_email);
  await page.getByLabel("Password", { exact: true }).fill(seed.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL((url) => !url.pathname.includes("/sign-in"));
  signedIn = await ctx.storageState();
  await ctx.close();
});

test.beforeEach(async ({ request }) => {
  await request.delete(`http://127.0.0.1:${enginePort}/fixture/uploads`);
});

async function tryAs(
  browser: Browser,
  baseURL: string,
  viewport: { width: number; height: number },
) {
  const ctx = await browser.newContext({
    baseURL,
    storageState: signedIn,
    viewport,
    hasTouch: viewport.width < 600,
  });
  return { ctx, page: await ctx.newPage() };
}

async function library(page: Page, kind: "image" | "video") {
  const response = await page.request.get(
    `/me/api/media?kind=${kind}&limit=40&scope=me`,
  );
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { assets: LibraryItem[] }).assets;
}

async function uploads(page: Page): Promise<Upload[]> {
  const response = await page.request.get(
    `http://127.0.0.1:${enginePort}/fixture/uploads`,
  );
  return ((await response.json()) as { uploads: Upload[] }).uploads;
}

/** What the engine must have received: the Library item's own file. */
async function expectEngineGot(page: Page, item: LibraryItem) {
  const file = await page.request.get(item.download_url);
  expect(file.ok()).toBe(true);
  const original = await file.body();
  expect(original.length).toBe(item.size_bytes);
  const sent = await uploads(page);
  expect(sent).toHaveLength(1);
  expect(sent[0].bytes).toBe(original.length);
  // Every byte, not a length and a prefix.
  expect(sent[0].sha256).toBe(createHash("sha256").update(original).digest("hex"));
  // A JSON note about the picture ("{"asset_id"…") is not the picture.
  expect(sent[0].head_hex).not.toBe(Buffer.from('{"asset_').toString("hex"));
}

/** The Library item behind the dialog tile at `index` (tiles are named by prompt). */
function itemFor(items: LibraryItem[], title: string) {
  const match = items.find((item) =>
    title.startsWith((item.prompt ?? "").trim().slice(0, 80)),
  );
  expect(match, `a Library item for tile "${title}"`).toBeTruthy();
  return match!;
}

async function openLibrary(page: Page, workflow: string) {
  await page.goto(`/try/?workflow=${encodeURIComponent(workflow)}`);
  const open = page.getByRole("button", { name: "Choose from Library" });
  await expect(open).toBeVisible();
  await open.click();
  return page.getByRole("dialog");
}

test("phone: a picture picked from the Library is shown and reaches the picture-edit engine as that picture", async ({
  browser,
  baseURL,
}) => {
  const { ctx, page } = await tryAs(browser, baseURL!, { width: 390, height: 844 });
  const items = await library(page, "image");
  const dialog = await openLibrary(page, "image.edit.qwen-image-2.1-edit");
  const tiles = dialog.locator("ul button");
  await expect(tiles).toHaveCount(items.length);
  // Every tile is its picture, not a "Preview unavailable" placeholder.
  await expect(dialog.getByText("Preview unavailable")).toHaveCount(0);
  await expect
    .poll(() =>
      dialog
        .locator("ul img")
        .evaluateAll((images) =>
          images.filter((image) => (image as HTMLImageElement).naturalWidth > 0).length,
        ),
    )
    .toBe(items.length);
  await page.screenshot({ path: "test-results/screens/library-pick-dialog-390.png" });

  // The third tile was made by another of Robin's apps, not by Try.
  const tile = tiles.nth(2);
  const item = itemFor(items, (await tile.getAttribute("aria-label")) ?? "");
  await tile.click();
  await expect(dialog).toBeHidden();
  const group = page.getByRole("group", { name: /Picture to change/ });
  await expect(group).toContainText("From your Library");
  await expect(group.getByRole("alert")).toHaveCount(0);
  await expect
    .poll(() =>
      group
        .getByRole("img", { name: "Chosen picture" })
        .evaluate((image) => (image as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0);
  await page.screenshot({ path: "test-results/screens/library-pick-chosen-390.png" });

  await page.getByRole("textbox", { name: /Describe the picture/ }).fill("Make it a painting");
  const sent = page.waitForResponse(
    (response) =>
      response.url().includes("/v1/images/edits") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  expect((await sent).ok()).toBe(true);
  await expectEngineGot(page, item);
  await ctx.close();
});

test("desktop: a Library picture sent inside the request for a picture-to-video workflow is the picture's own data", async ({
  browser,
  baseURL,
}) => {
  const { ctx, page } = await tryAs(browser, baseURL!, { width: 1280, height: 900 });
  const items = await library(page, "image");
  const dialog = await openLibrary(page, "video.generate.ltx-2.5-i2v");
  const tile = dialog.locator("ul button").nth(4);
  const item = itemFor(items, (await tile.getAttribute("aria-label")) ?? "");
  await expect(dialog.getByText("Preview unavailable")).toHaveCount(0);
  await page.screenshot({ path: "test-results/screens/library-pick-dialog-1280.png" });
  await tile.click();
  await expect(dialog).toBeHidden();
  const group = page.getByRole("group", { name: /Starting picture/ });
  await expect(group).toContainText("From your Library");
  await expect
    .poll(() =>
      group
        .getByRole("img", { name: "Chosen picture" })
        .evaluate((image) => (image as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0);

  await page.getByRole("textbox", { name: /What to make/ }).fill("A slow zoom in");
  const request = page.waitForRequest(
    (value) => value.url().includes("/v1/videos") && value.method() === "POST",
  );
  const response = page.waitForResponse(
    (value) => value.url().includes("/v1/videos") && value.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run", exact: true }).click();
  // The request carries the picture, not an id the Try key may not read.
  expect((await request).postDataJSON().input_reference).toMatch(/^data:image\//);
  expect((await response).ok()).toBe(true);
  await expectEngineGot(page, item);
  await ctx.close();
});

test("phone: a video picked from the Library reaches a video-edit engine as that video", async ({
  browser,
  baseURL,
}) => {
  const { ctx, page } = await tryAs(browser, baseURL!, { width: 390, height: 844 });
  const items = await library(page, "video");
  expect(items.length).toBeGreaterThan(0);
  const dialog = await openLibrary(page, "video.edit.rife-interpolate");
  const tile = dialog.locator("ul button").first();
  await expect(dialog.getByText("Preview unavailable")).toHaveCount(0);
  // The tile plays the item's signed link, not the download route's JSON.
  await expect(tile.locator("video")).toHaveAttribute("src", /^https?:\/\/.+\/v1\/assets\//);
  const item = itemFor(items, (await tile.getAttribute("aria-label")) ?? "");
  await tile.click();
  await expect(dialog).toBeHidden();
  const group = page.getByRole("group", { name: /Video to smooth/ });
  await expect(group).toContainText("From your Library");
  await expect(group.getByRole("alert")).toHaveCount(0);

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect.poll(async () => (await uploads(page)).length).toBe(1);
  await expectEngineGot(page, item);
  await ctx.close();
});
