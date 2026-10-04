import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("signed audio plays and seeks through the gateway delivery route", async ({
  page,
}) => {
  const { local_asset } = JSON.parse(
    await readFile("test-results/live-data-manifest.json", "utf8"),
  );
  const response = await page.request.get(
    `/me/api/media/${local_asset}/download`,
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

  await page.goto("/ui/library/");
  await page.getByRole("button", { name: "Audio", exact: true }).click();
  const audio = page.locator("article audio").first();
  await expect(audio).toBeVisible();
  await audio.evaluate(async (element: HTMLAudioElement) => {
    element.muted = true;
    await element.play();
  });
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(0.1);
  await expect
    .poll(() => audio.evaluate((element: HTMLAudioElement) => element.duration))
    .toBeGreaterThan(2.5);
  await audio.evaluate((element: HTMLAudioElement) => {
    element.pause();
    element.currentTime = 1.8;
  });
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(1.7);
  await audio.evaluate(async (element: HTMLAudioElement) => {
    await element.play();
  });
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
