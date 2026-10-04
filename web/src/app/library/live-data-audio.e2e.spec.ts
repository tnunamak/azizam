import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

async function seriousAxeViolations(page: Page) {
  return (await new AxeBuilder({ page }).analyze()).violations
    .filter((violation) =>
      ["serious", "critical"].includes(violation.impact ?? ""),
    )
    .map((violation) => violation.id);
}

test("library mini audio player handles live-data audio playback", async ({
  page,
}) => {
  const { local_asset } = JSON.parse(
    await readFile("test-results/library-live-data-manifest.json", "utf8"),
  );

  const response = await page.request.get(
    `/me/api/media/${local_asset}/download`,
  );
  expect(response.status()).toBe(200);
  const { download_url } = await response.json();

  await page.goto("/ui/library/");
  await page.getByRole("button", { name: "Audio", exact: true }).click();

  const card = page
    .locator("article")
    .filter({ hasText: "Audio · Prompt not recorded" });
  await expect(card).toBeVisible();
  await expect(card.getByText("voice")).toBeVisible();

  const playButton = card.getByRole("button", {
    name: "Play Audio · Prompt not recorded",
  });
  await expect(playButton).toBeVisible();
  const buttonBox = await playButton.boundingBox();
  expect(buttonBox?.width).toBeGreaterThanOrEqual(44);
  expect(buttonBox?.height).toBeGreaterThanOrEqual(44);

  const audio = card.locator("audio");
  await expect(audio).toHaveAttribute("preload", "metadata");
  await expect(audio).not.toBeVisible();
  await expect
    .poll(() => audio.evaluate((element: HTMLAudioElement) => element.duration))
    .toBeGreaterThan(2.5);
  await expect(card.getByText("0:03")).toBeVisible();

  await audio.evaluate((element: HTMLAudioElement) => {
    element.muted = true;
  });
  await playButton.click();
  await expect(
    card.getByRole("button", { name: "Pause Audio · Prompt not recorded" }),
  ).toBeVisible();
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(0.1);

  await card
    .getByRole("button", { name: "Pause Audio · Prompt not recorded" })
    .click();
  await expect(
    card.getByRole("button", { name: "Play Audio · Prompt not recorded" }),
  ).toBeVisible();

  await audio.evaluate((element: HTMLAudioElement) => {
    element.currentTime = 1.8;
  });
  await expect
    .poll(() =>
      audio.evaluate((element: HTMLAudioElement) => element.currentTime),
    )
    .toBeGreaterThan(1.7);

  await card
    .getByRole("button", { name: "Play Audio · Prompt not recorded" })
    .click();
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

  await audio.evaluate((element: HTMLAudioElement) => {
    element.pause();
    element.src = "/me/api/media/missing-audio/download";
    element.load();
  });
  await expect(card.getByText("Audio unavailable")).toBeVisible();
  await card
    .getByRole("button", { name: "Play Audio · Prompt not recorded" })
    .click();
  await expect(card.getByText("Audio unavailable")).toBeVisible();

  const badResponse = await page.request.get(
    `${download_url.replace(/.$/, "x")}`,
  );
  expect(badResponse.status()).toBeGreaterThanOrEqual(400);
  expect(await seriousAxeViolations(page)).toEqual([]);
});
