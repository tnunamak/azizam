import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

const png =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=";

test("a long picture finishes elsewhere in the app and opens with a fresh URL", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await mkdir("/home/tnunamak/.tmp/longjobs-shots", { recursive: true });
  let started = false;
  let finished = false;
  let renewals = 0;
  const polls: { since: string | null; polledAt: number }[] = [];
  const job = () => ({
    job_id: "mj_long",
    intent: "image.generate",
    status: finished ? "succeeded" : "running",
    created_at: Math.floor(Date.now() / 1000),
    updated_at: Math.floor(Date.now() / 1000),
    assets: finished ? [{ asset_id: "img_long" }] : [],
  });
  const asset = {
    asset_id: "img_long",
    kind: "image",
    prompt: "A quiet lake",
    created_at: Math.floor(Date.now() / 1000),
    download_url: "/expired-picture?expires=1",
    available: true,
  };
  await page.route(
    /\/me\/api\/jobs(?:\?.*)?$/,
    (route) => {
      const params = new URL(route.request().url()).searchParams;
      expect(params.get("watch")).toBe("1");
      const polledAt = Math.floor(Date.now() / 1000);
      polls.push({ since: params.get("since"), polledAt });
      return route.fulfill({
        json: {
          jobs: started ? [job()] : [],
          next_cursor: null,
          polled_at: polledAt,
        },
      });
    },
  );
  await page.route("**/gateway/v1/media/jobs/image-generations", (route) => {
    started = true;
    return route.fulfill({ status: 202, json: { ...job(), id: "mj_long" } });
  });
  await page.route("**/gateway/v1/media/jobs/mj_long", (route) =>
    route.fulfill({ json: { ...job(), id: "mj_long" } }),
  );
  await page.route("**/me/api/media?*", (route) =>
    route.fulfill({
      json: {
        assets: finished ? [asset] : [],
        sources: [],
        models: [],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/me/api/media/img_long", (route) =>
    route.fulfill({ json: { asset } }),
  );
  await page.route("**/me/api/media/img_long/download?*", (route) => {
    renewals++;
    return route.fulfill({ json: { download_url: png } });
  });
  await page.goto("/try/");
  // The job starts after an empty poll and finishes on another page. The
  // shell must discover it through its timer and the server's watermark.
  await expect.poll(() => polls.length).toBe(1);
  expect(polls[0].since).toBeNull();
  await page.getByRole("tab", { name: "Make a picture" }).click();
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect.poll(() => started).toBe(true);
  await page.getByRole("link", { name: "Status", exact: true }).first().click();
  await expect(page).toHaveURL(/\/status\//);
  finished = true;
  const notice = page.getByRole("region", { name: "Picture ready" });
  await expect(notice).toBeVisible({ timeout: 75_000 });
  expect(polls.length).toBeGreaterThanOrEqual(2);
  expect(polls[1].since).toBe(String(polls[0].polledAt));
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((value) => {
        document.documentElement.classList.toggle("dark", value === "dark");
        document.documentElement.style.colorScheme = value;
      }, theme);
      for (const scale of [100, 200]) {
        await page.evaluate((value) => {
          document.documentElement.style.fontSize = `${value}%`;
        }, scale);
        await notice.scrollIntoViewIfNeeded();
        await expect(notice).toBeVisible();
        const box = await notice.boundingBox();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
        for (const control of await notice.locator("a, button").all()) {
          const bounds = await control.boundingBox();
          expect(bounds!.height).toBeGreaterThanOrEqual(44);
          expect(bounds!.width).toBeGreaterThanOrEqual(44);
        }
        expect(
          (
            await new AxeBuilder({ page })
              .include('[aria-label="Job updates"]')
              .analyze()
          ).violations,
        ).toEqual([]);
        await page.screenshot({
          path: `/home/tnunamak/.tmp/longjobs-shots/notice-${width}-${theme}-${scale}.png`,
        });
      }
    }
  }
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "100%";
  });
  await notice.getByRole("link", { name: "Open result" }).click();
  await expect(page).toHaveURL(/asset=img_long/);
  const viewer = page.getByRole("dialog");
  await expect(
    viewer.getByRole("img", { name: "A quiet lake" }),
  ).toHaveAttribute("src", png);
  expect(renewals).toBe(1);
  await expect(notice).toBeHidden();
  await page.reload();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(notice).toBeHidden();
  await page.getByRole("button", { name: "Close", exact: true }).first().click();
  await page.getByRole("link", { name: "Status", exact: true }).first().click();
  await expect(notice).toBeHidden();
});
