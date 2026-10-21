import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { fulfilJson } from "./fixtures/intercept";

test("signed audio plays and seeks through the gateway delivery route", async ({
  page,
}) => {
  const { local_asset } = JSON.parse(
    await readFile("test-results/live-data-manifest.json", "utf8"),
  );
  const response = await page.request.get(
    `/me/api/media/${local_asset}/download?scope=everyone`,
  );
  expect(response.status()).toBe(200);
  const { download_url } = await response.json();
  const chunk = await page.request.get(download_url, {
    headers: { range: "bytes=44-2047" },
  });
  expect(chunk.status()).toBe(206);
  expect(chunk.headers()["content-type"]).toContain("audio/wav");
  expect(chunk.headers()["content-range"]).toMatch(/^bytes 44-2047\/\d+$/);
  expect(chunk.headers()["accept-ranges"]).toBe("bytes");
  const tampered = new URL(download_url);
  tampered.searchParams.set("md", "full");
  expect((await page.request.get(tampered.toString())).status()).toBe(403);

  // Choosing a kind refreshes the list. A refresh signs every URL again, and a
  // tile whose URL changed is replaced, which ends a playing track. Answer
  // that refresh late with new URLs, so the test meets the replacement on
  // every run and must wait for it.
  await page.route("**/me/api/media?*kind=audio*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await new Promise((resolve) => setTimeout(resolve, 500));
    for (const asset of body.assets) asset.download_url += "&refresh=1";
    await fulfilJson(route, body, { response });
  });
  const firstList = page.waitForResponse((candidate) =>
    candidate.url().includes("/me/api/media?limit=40&scope=everyone"),
  );
  await page.goto("/library/");
  await (await firstList).finished();
  const refreshed = page.waitForResponse((candidate) =>
    candidate.url().includes("kind=audio"),
  );
  await page.getByRole("button", { name: "Audio", exact: true }).click();
  await (await refreshed).finished();
  // The grid is busy while the old tiles stand in for the refresh.
  await expect(page.locator("[aria-busy]:has(article)")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  const audio = page.locator("article audio").first();
  await expect(audio).toBeAttached();
  await expect(audio).toHaveAttribute("preload", "none");
  const player = audio.locator("..");
  const play = player.getByRole("button", { name: /^Play / });
  await expect(play).toBeVisible();
  await audio.evaluate((element: HTMLAudioElement) => {
    element.muted = true;
  });
  await play.click();
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(0.1);
  await expect
    .poll(() => audio.evaluate((element: HTMLAudioElement) => element.duration))
    .toBeGreaterThan(2.5);
  await player.getByRole("button", { name: /^Pause / }).click();
  await expect(player.getByRole("button", { name: /^Play / })).toBeVisible();
  await audio.evaluate((element: HTMLAudioElement) => {
    element.currentTime = 1.8;
  });
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(1.7);
  await player.getByRole("button", { name: /^Play / }).click();
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(2);
  expect(
    await audio.evaluate(
      (element: HTMLAudioElement) => element.error?.code ?? null,
    ),
  ).toBeNull();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
