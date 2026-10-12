import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// /showcase/ is the gateway's static product page (showcase/ in the repo), not
// a web app page. These tests drive it through the real gateway.

const sections = {
  sections: [
    { id: "chat", title: "Chat and vision", source: "aliases:chat" },
    { id: "speech", title: "Speech", source: "aliases:speech" },
    {
      id: "images",
      title: "Image generation",
      match: { intent: "image.generate" },
    },
    { id: "more", title: "More workflows", fallback: true },
  ],
};

type Example = Record<string, unknown>;

const recorded = (id: string, extra: Example): Example => ({
  id,
  base: `/showcase/examples/${id}/`,
  card: "chat",
  ...extra,
});

/** Serve sections and the examples index; `data` returning null answers 503. */
async function serveExamples(page: Page, data: () => Example[] | null) {
  await page.route("**/showcase/static/sections.json", (route) =>
    route.fulfill({ json: sections }),
  );
  await page.route("**/showcase/examples/index.json", (route) => {
    const list = data();
    return list
      ? route.fulfill({ json: { object: "list", data: list } })
      : route.fulfill({ status: 503, json: { error: { code: "unavailable" } } });
  });
}

const answer = recorded("chat-recorded", {
  title: "A recorded answer",
  model: "local-chat",
  seconds: 1.2,
  inputs: [{ kind: "text", value: "Why is the sky blue?" }],
  outputs: [{ kind: "text", text: "Air scatters blue light." }],
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 390]) {
    test(`showcase: ${theme}, ${width}px`, async ({ page }) => {
      const exceptions: string[] = [];
      page.on("pageerror", (error) => exceptions.push(error.message));
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await serveExamples(page, () => [answer]);
      const response = await page.goto("/showcase/");
      expect(response?.status()).toBe(200);
      expect(response?.headers()["cache-control"]).toBe("no-cache");
      await expect(page).toHaveTitle("Azizam: your own AI gateway");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.getByText("Air scatters blue light.")).toBeVisible();
      await expect(
        page.getByRole("navigation", { name: "Main navigation" }),
      ).toHaveCount(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      const result = await new AxeBuilder({ page }).analyze();
      expect(
        result.violations.filter((v) =>
          ["serious", "critical"].includes(v.impact ?? ""),
        ),
      ).toEqual([]);
      expect(exceptions).toEqual([]);
      await page.screenshot({
        path: `test-results/screens/public-showcase-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("/showcase redirects to the static page and keeps the query", async ({
  request,
}) => {
  const moved = await request.get("/showcase?from=link", { maxRedirects: 0 });
  expect(moved.status()).toBe(308);
  expect(moved.headers()["location"]).toBe("/showcase/?from=link");
  const head = await request.head("/showcase/");
  expect(head.status()).toBe(200);
  expect(head.headers()["content-type"]).toContain("text/html");
});

test("showcase retries a failed recorded-example request", async ({ page }) => {
  let unavailable = true;
  await serveExamples(page, () => (unavailable ? null : [answer]));
  await page.goto("/showcase/");
  const banner = page.locator("#banner");
  await expect(banner).toContainText(
    "Recorded examples could not be loaded from the gateway.",
  );
  unavailable = false;
  await banner.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("Air scatters blue light.")).toBeVisible();
  await expect(banner).not.toContainText("Recorded examples could not");
});

test("showcase marks broken recordings and shows recorded falsy inputs", async ({
  page,
}) => {
  let repaired = false;
  const requested: string[] = [];
  page.on("request", (request) => requested.push(request.url()));
  await serveExamples(page, () => [
    recorded("invalid", {
      title: "Unavailable recording",
      inputs: [{ value: null }, { value: "   " }],
      outputs: repaired
        ? [{ kind: "text", text: "Recovered recording" }]
        : [
            null,
            { kind: "image", file: "../private.png" },
            { kind: "text", text: " " },
          ],
    }),
    recorded("recorded", {
      title: "Recorded values",
      inputs: [{ value: 0 }, { value: false }],
      outputs: [{ kind: "text", text: "Saved answer" }],
    }),
  ]);
  await page.goto("/showcase/");
  await expect(
    page.getByText("This recording could not be loaded."),
  ).toHaveCount(3);
  await expect(page.getByText("Input was not recorded.")).toHaveCount(1);
  await expect(page.locator("pre.in-text")).toHaveText(["0", "false"]);
  expect(requested.filter((url) => url.includes("private.png"))).toEqual([]);
  repaired = true;
  await page
    .locator(".load-failed")
    .first()
    .getByRole("button", { name: "Try again", exact: true })
    .click();
  await expect(page.getByText("Recovered recording")).toBeVisible();
  await expect(
    page.getByText("This recording could not be loaded."),
  ).toHaveCount(0);
});

test("showcase shows a structured recorded request as JSON", async ({ page }) => {
  const exceptions: string[] = [];
  page.on("pageerror", (error) => exceptions.push(error.message));
  await serveExamples(page, () => [
    recorded("structured", {
      title: "Recorded structured request",
      inputs: [
        {
          value: {
            model: "fast",
            max_tokens: 32,
            messages: [{ role: "user", content: "Hello" }],
          },
        },
      ],
      outputs: [{ kind: "text", text: "Recorded answer" }],
    }),
  ]);
  await page.goto("/showcase/");
  await expect(page.getByText("Recorded answer", { exact: true })).toBeVisible();
  const request = page.locator("pre.in-text");
  await expect(request).toContainText('"model": "fast"');
  await expect(request).toContainText('"messages": [');
  expect(exceptions).toEqual([]);
});

test("showcase retries an image that failed to load", async ({ page }) => {
  let unavailable = true;
  let attempts = 0;
  await serveExamples(page, () => [
    recorded("image-recorded", {
      outputs: [{ kind: "image", file: "output.png", label: "Recorded picture" }],
    }),
  ]);
  await page.route("**/showcase/examples/image-recorded/output.png", (route) => {
    attempts++;
    return unavailable
      ? route.fulfill({ status: 503, body: "" })
      : route.fulfill({
          contentType: "image/png",
          body: Buffer.from(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=",
            "base64",
          ),
        });
  });
  await page.goto("/showcase/");
  // Images load lazily: bring the recording into view.
  await page.locator(".recorded-media").first().scrollIntoViewIfNeeded();
  const failed = page.getByText("This recording could not be loaded.");
  await expect(failed).toBeVisible();
  unavailable = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  const picture = page.getByRole("img", { name: "Generated image" });
  await expect(picture).toBeVisible();
  await expect
    .poll(() => picture.evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBe(1);
  expect(attempts).toBeGreaterThanOrEqual(2);
  await expect(failed).toHaveCount(0);
});

for (const kind of ["audio", "video"] as const) {
  test(`showcase can retry repeated ${kind} playback errors`, async ({ page }) => {
    await serveExamples(page, () => [
      recorded("media-recorded", {
        outputs: [{ kind, file: "output.media", label: "Recorded media" }],
      }),
    ]);
    await page.goto("/showcase/", { waitUntil: "domcontentloaded" });
    const failed = page.getByText("This recording could not be loaded.");
    for (let attempt = 0; attempt < 2; attempt++) {
      const media = page.locator(`.recorded-media ${kind}`);
      await expect(media).toHaveCount(1);
      await media.dispatchEvent("error");
      await expect(failed).toBeVisible();
      await page.getByRole("button", { name: "Try again", exact: true }).click();
      await expect(media).toHaveCount(1);
      await expect(failed).toHaveCount(0);
    }
  });
}
