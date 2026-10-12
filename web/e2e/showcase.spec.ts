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
      : route.fulfill({
          status: 503,
          json: { error: { code: "unavailable" } },
        });
  });
}

const answer = recorded("chat-recorded", {
  title: "A recorded answer",
  model: "local-chat",
  seconds: 1.2,
  inputs: [{ kind: "text", value: "Why is the sky blue?" }],
  outputs: [{ kind: "text", text: "Air scatters blue light." }],
});

// Display states (retry, broken recordings, structured input, media errors) are
// covered in jsdom by web/tests/showcase-page.spec.ts. The browser keeps one
// representative per theme and width for layout, axe and the real HTTP headers.
for (const [theme, width] of [
  ["light", 1440],
  ["dark", 390],
] as const) {
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
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
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
