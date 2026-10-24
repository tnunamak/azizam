import { mkdirSync } from "node:fs";
import path from "node:path";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { expect, ownerApi, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";
import { seedFirstVisitMember, seedHistoryNoticeDue, seedOwner, seedReturningMember } from "./fixtures/home-seed";

/*
 * The signed-in Home, end to end: the composer and chooser, Make's hand-off to Try, Recent, the
 * owner's panel, the access-plan notice, and the layout gates. Real data comes from the isolated
 * fixture (a real member with a real key); a stub is used only for the states a real gateway
 * cannot reach on demand, and fulfilJson checks each stub against the real gateway's shape.
 */

/** A route that waits until released, then continues to the real gateway. */
function holdRoute() {
  let release!: () => void;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { released, release };
}

/** The request paths a page made to the member API or the gateway's data plane, in order. */
function watchApi(page: Page) {
  const paths: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/^\/(me\/api|gateway\/v1)\//.test(url.pathname)) paths.push(url.pathname + url.search);
  });
  return paths;
}

const primary = (page: Page) => page.locator("main [data-variant='default']");

test("H1 H12 a member's cold load makes at most five Home requests, and typing waits for none of them", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  const paths = watchApi(page);
  const home = holdRoute();
  await page.route("**/me/api/home", async (route) => {
    await home.released;
    await route.continue();
  });
  await page.goto("/");
  // The box is one element for the whole page; its name changes once the chooser loads, so it is found by id.
  const box = page.locator("#home-prompt");
  await box.fill("A lake at dawn");
  await expect(box).toHaveValue("A lake at dawn");
  await expect(page.getByRole("button", { name: "Make" })).toBeDisabled();
  home.release();
  await expect(page.getByRole("button", { name: /^(Make|Send)$/ })).toBeEnabled();
  await expect(box).toHaveValue("A lake at dawn");
  const counted = paths.filter((item) => item.startsWith("/me/api/") || item.startsWith("/gateway/v1/"));
  // Four for Home itself, and the request history notice's own read of /me/api/retention (r103).
  expect(counted.length).toBeLessThanOrEqual(5);
  expect(counted.filter((item) => item.startsWith("/me/api/home")).length).toBe(1);
  expect(counted.filter((item) => item.startsWith("/gateway/v1/"))).toEqual([]);
});

test("H5 Make hands the work to Try, the words stay out of the address, and Back returns to an empty Home", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.goto("/");
  const box = page.getByRole("textbox", { name: /Describe|Ask/ });
  await box.fill("A red fox in fresh snow");
  const run = page.waitForRequest((request) => request.method() === "POST" && /chat\/completions|images|media\/jobs/.test(request.url()));
  await page.getByRole("button", { name: /^(Make|Send)$/ }).click();
  await page.waitForURL(/\/try\/\?/);
  expect(decodeURIComponent(page.url())).not.toContain("red fox");
  expect(new URL(page.url()).searchParams.get("capability")).toBeTruthy();
  const request = await run;
  expect(request.postData() ?? "").toContain("red fox in fresh snow");
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("textbox", { name: /Describe|Ask/ })).toHaveValue("");
});

test("H6 a make in progress is first in Recent, and a finished picture opens Library's viewer", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  const real = await (await member.context.request.get("/me/api/home")).json();
  const now = Date.now() / 1000;
  const chatRow = real.capabilities.find((row: { icon_key?: string }) => row.icon_key === "chat") ?? real.capabilities[0];
  const imageRow = {
    ...chatRow,
    icon_key: "image",
    capability: "image",
    capability_title: "Images",
    title: "Krea 2 Turbo",
    use_name: "krea-2-turbo",
    recommended_model: "krea-2-turbo",
    intent: "image.generate",
    typical_seconds: 9,
  };
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
      ...real,
      capabilities: [...real.capabilities, imageRow],
      receipts: [
        {
          request_id: "req-making",
          intent: "image.generate",
          path: "/gateway/v1/media/jobs/image-generations",
          status: "running",
          accepted_at_ms: (now - 2) * 1000,
        },
      ],
    }),
  );
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, {
      assets: [{ asset_id: "asset-finished", kind: "image", prompt: "Raised bed, late afternoon", download_url: "/none.png", created_at: now - 3600 }],
      has_more: false,
      next_cursor: null,
    }),
  );
  await page.goto("/");
  const recent = page.getByRole("list", { name: "Recent work" });
  await expect(recent.getByRole("listitem").first()).toHaveAttribute("data-making", "true");
  await expect(recent.getByText(/about \d+ s left/i)).toBeVisible();
  const finished = page.getByRole("link", { name: /Raised bed, late afternoon/ });
  await expect(finished).toHaveAttribute("href", /\/library\/?\?asset=asset-finished/);
});

test("H6 five tiles fill the row at 1100 px, with no gap at its end", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  // From 1024 px Recent has one column per tile, so five tiles fill the row.
  await page.setViewportSize({ width: 1100, height: 900 });
  const real = await (await member.context.request.get("/me/api/home")).json();
  const now = Date.now() / 1000;
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
      ...real,
      receipts: [
        { request_id: "req-fill", intent: "image.generate", path: "/gateway/v1/media/jobs/image-generations", status: "running", accepted_at_ms: (now - 2) * 1000 },
      ],
      recent_chats: [{ request_id: "chat-fill", record_id: "chat-fill-record", created_at: now - 10_800, title: "Plan a two-day coastal trip" }],
    }),
  );
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, {
      assets: [1, 2, 3].map((index) => ({ asset_id: `asset-fill-${index}`, kind: "image", prompt: `Garden ${index}`, download_url: "/none.png", created_at: now - 3600 * index })),
      has_more: false,
      next_cursor: null,
    }),
  );
  await page.goto("/");
  const recent = page.getByRole("list", { name: "Recent work" });
  const tiles = recent.getByRole("listitem");
  await expect(tiles).toHaveCount(5);
  const boxes = await tiles.evaluateAll((items) => items.map((item) => item.getBoundingClientRect().toJSON()));
  const list = await recent.boundingBox();
  expect(new Set(boxes.map((box) => Math.round(box.top))).size).toBe(1);
  expect(list).not.toBeNull();
  expect(Math.abs(Math.max(...boxes.map((box) => box.right)) - (list!.x + list!.width))).toBeLessThanOrEqual(2);
});

test("at 1440 px Recent fills Home's 960 px column, and the 760 px composer sits centred over it", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 1440, height: 900 });
  await seedReturningMember(page, member.context.request);
  await page.goto("/");
  const tiles = page.getByRole("list", { name: "Recent work" }).getByRole("listitem");
  await expect(tiles).toHaveCount(6);
  const boxes = await tiles.evaluateAll((items) => items.map((item) => item.getBoundingClientRect().toJSON()));
  const composer = await page.locator("[data-slot='composer']").boundingBox();
  expect(composer).not.toBeNull();
  // One centred column: the row spans 960, and the composer is inset by the same amount on both sides.
  expect(Math.round(boxes[5].right - boxes[0].left)).toBe(960);
  expect(Math.round(composer!.width)).toBe(760);
  const leftInset = composer!.x - boxes[0].left;
  const rightInset = boxes[5].right - (composer!.x + composer!.width);
  expect(Math.abs(leftInset - rightInset)).toBeLessThanOrEqual(1);
  // The sizes the design names, as the browser computes them: a 16 px prompt, 13/18 titles of one or two
  // lines, and 12 px "Kind · when".
  const sizes = await page.evaluate(() => {
    const px = (element: Element | null) => (element ? getComputedStyle(element) : null);
    const tile = document.querySelector("[data-slot='recent-tile']");
    const spans = tile ? Array.from(tile.querySelectorAll("p, span")) : [];
    const title = spans.find((node) => node.className.includes("line-clamp-2")) ?? null;
    const meta = spans.find((node) => node.className.includes("text-badge")) ?? null;
    return {
      prompt: px(document.getElementById("home-prompt"))?.fontSize,
      title: px(title)?.fontSize,
      titleLine: px(title)?.lineHeight,
      meta: px(meta)?.fontSize,
    };
  });
  expect(sizes).toEqual({ prompt: "16px", title: "13px", titleLine: "18px", meta: "12px" });
  // Each "Kind · when" sits 2 px under its own title, and every one in the row shares a baseline. The row is as
  // tall as its tallest caption, so a one-line title beside a two-line title sits lower, with its line under it.
  const captions = await tiles.evaluateAll((items) =>
    items.map((item) => {
      const title = item.querySelector(".line-clamp-2")?.getBoundingClientRect();
      const metas = Array.from(item.querySelectorAll("span, p")).filter((node) => node.className.includes("text-badge"));
      const meta = metas[metas.length - 1]?.getBoundingClientRect();
      return { gap: title && meta ? meta.top - title.bottom : -1, bottom: meta ? Math.round(meta.bottom) : -1 };
    }),
  );
  expect(captions.map((caption) => caption.gap)).not.toContain(-1);
  for (const caption of captions) expect(Math.abs(caption.gap - 2)).toBeLessThanOrEqual(0.5);
  expect(captions.map((caption) => caption.bottom)).not.toContain(-1);
  expect(new Set(captions.map((caption) => caption.bottom)).size).toBe(1);
});

test("H4 the facts line shows the cost and wait its row knows, and leaves out what it does not", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  const real = await (await member.context.request.get("/me/api/home")).json();
  const chatRow = real.capabilities.find((row: { icon_key?: string }) => row.icon_key === "chat") ?? real.capabilities[0];
  // The route reads this at each load, so one page can show each case in turn.
  let row: Record<string, unknown> = { ...chatRow, cost_hint: { kind: "free", price_usd: null }, typical_seconds: 1 };
  await page.route("**/me/api/home", (route) => fulfilJson(route, { ...real, capabilities: [row] }));
  await page.goto("/");
  await expect(page.getByText("No charge · starts in about 1 s")).toBeVisible();
  row = { ...chatRow, cost_hint: { kind: "unknown", price_usd: null }, typical_seconds: 1 };
  await page.reload();
  // The composer's line is a paragraph; the first-visit list also names a wait, as a span, so the match is scoped.
  await expect(page.getByRole("paragraph").filter({ hasText: /^Starts in about 1 s$/ })).toBeVisible();
  row = { ...chatRow, cost_hint: { kind: "unknown", price_usd: null }, typical_seconds: null };
  await page.reload();
  await expect(page.getByRole("button", { name: /^(Make|Send)$/ })).toBeVisible();
  await expect(page.getByRole("paragraph").filter({ hasText: /^(No charge|Starts in|About |Paid model)/ })).toHaveCount(0);
  await expect(page.getByText(/unknown/i)).toHaveCount(0);
});

test("H7 a failed Recent leaves the composer working, and Try again refetches only Recent", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  const paths = watchApi(page);
  let media = 0;
  let failing = true;
  await page.route("**/me/api/media?limit=8", async (route) => {
    media += 1;
    if (failing) return fulfilJson(route, { error: { code: "unavailable", message: "Library unavailable" } }, { status: 503 });
    await route.continue();
  });
  await page.goto("/");
  // The app retries a server error twice before it shows one, so the error appears after those attempts.
  await expect(page.getByText("Couldn’t load your recent work.")).toBeVisible();
  await page.getByRole("textbox", { name: /Describe|Ask/ }).fill("A quiet room");
  await expect(page.getByRole("button", { name: /^(Make|Send)$/ })).toBeEnabled();
  const homeBefore = paths.filter((item) => item.startsWith("/me/api/home")).length;
  const mediaBefore = media;
  failing = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => media).toBeGreaterThan(mediaBefore);
  expect(paths.filter((item) => item.startsWith("/me/api/home")).length).toBe(homeBefore);
});

test("H8 with one kind to make and no work, Home shows the composer's single chip and no cards", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.goto("/");
  await expect(page.getByRole("group", { name: "What to make" }).getByRole("button")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "What you can make here" })).toHaveCount(0);
});

test("H8 with no work, Home offers what can be made, and choosing a row focuses the box", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await seedFirstVisitMember(page, member.context.request);
  await page.goto("/");
  const list = page.getByRole("region", { name: "What you can make here" });
  await expect(list).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent" })).toHaveCount(0);
  await list.getByRole("button").first().click();
  await expect(page.getByRole("textbox", { name: /Describe|Ask/ })).toBeFocused();
});

test("at 1440 px a first visit's cards keep the composer's 760 px edges, under a centred 13 px line", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 1440, height: 900 });
  await seedFirstVisitMember(page, member.context.request);
  await page.goto("/");
  const group = page.getByRole("region", { name: "What you can make here" }).getByRole("group", { name: "What to make" });
  await expect(group).toBeVisible();
  const composer = (await page.locator("[data-slot='composer']").boundingBox())!;
  const row = (await group.boundingBox())!;
  expect(Math.abs(row.x - composer.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(row.x + row.width - (composer.x + composer.width))).toBeLessThanOrEqual(1);
  const lead = page.getByText("Sam set up this gateway for the household.");
  const box = (await lead.boundingBox())!;
  const text = await lead.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    return { center: rect.left + rect.width / 2, size: getComputedStyle(node).fontSize };
  });
  expect(text.size).toBe("13px");
  expect(Math.abs(text.center - (composer.x + composer.width / 2))).toBeLessThanOrEqual(1);
  expect(Math.round(row.y - (box.y + box.height))).toBe(16);
});

test("at 768 px a first visit's chosen ring stays inside its card, and every caption ends on one line", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 768, height: 1024 });
  await seedFirstVisitMember(page, member.context.request);
  await page.goto("/");
  const group = page.getByRole("region", { name: "What you can make here" }).getByRole("group", { name: "What to make" });
  const chat = group.getByRole("button", { name: /^Chat Starts in/ });
  await chat.click();
  await expect(chat).toHaveAttribute("aria-pressed", "true");
  const shape = await group.evaluate((node) => {
    const cards = Array.from(node.querySelectorAll<HTMLElement>("button[aria-pressed]"));
    const ring = cards[0].querySelector<HTMLElement>("span.absolute.inset-0");
    const faces = cards.map((card) => card.firstElementChild!.getBoundingClientRect());
    return {
      ring: ring ? getComputedStyle(ring).boxShadow : "",
      outline: getComputedStyle(cards[0]).outlineStyle,
      faceHeights: new Set(faces.map((face) => Math.round(face.height))).size,
      captionEnds: new Set(cards.map((card) => Math.round(card.getBoundingClientRect().bottom))).size,
      grid: node.getBoundingClientRect().toJSON(),
      cardBoxes: cards.map((card) => card.getBoundingClientRect().toJSON()),
    };
  });
  // The ring is an inset shadow on the face, so nothing is drawn outside the grid's edges.
  expect(shape.ring).toContain("inset");
  expect(shape.faceHeights).toBe(1);
  expect(shape.captionEnds).toBe(1);
  for (const card of shape.cardBoxes) {
    expect(card.left).toBeGreaterThanOrEqual(shape.grid.left - 0.5);
    expect(card.right).toBeLessThanOrEqual(shape.grid.right + 0.5);
  }
});

test("H11 without an access plan the composer is absent, and Ask posts once and confirms in place", async ({ isolated, request }) => {
  const member = await isolated.member();
  const { page } = member;
  // The owner takes the plan away through the same admin API an owner uses.
  const owner = await ownerApi(request);
  await owner.send("PATCH", `/admin/api/people/${member.userId}`, { access_plan: null });
  const paths = watchApi(page);
  await page.goto("/");
  await expect(page.getByText("You can’t make anything yet.")).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.getByRole("button", { name: /^Ask / }).click();
  await expect(page.getByText(/^Sent to /)).toBeVisible();
  expect(paths.filter((item) => item === "/me/api/access-request").length).toBe(1);
});

test("H9 the owner's panel holds operations only, each with one link to where it is fixed", async ({ page }) => {
  await page.goto("/");
  const panel = page.getByRole("region", { name: "Your gateway" });
  await expect(panel).toBeVisible();
  // The fixture's owner has capabilities that are off, so the panel names them in one row that opens Engines.
  await expect(panel.getByRole("link", { name: /Open Engines$/ }).first()).toBeVisible();
  const links = panel.getByRole("link");
  const hrefs = await links.evaluateAll((items) => items.map((item) => item.getAttribute("href") ?? ""));
  for (const href of hrefs) expect(href).toMatch(/^\/(engines|providers|status|people)\b/);
  await expect(panel).not.toContainText(/prompt|photo|picture/i);
});

test("at 1440 px the owner's panel is a quiet muted block, and the fault shows there, not as a dot on the chooser", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await seedOwner(page, page.context().request);
  await page.goto("/");
  const panel = page.getByRole("region", { name: "Your gateway" });
  await expect(panel.getByRole("link", { name: /^Video is down/ })).toBeVisible();
  // One line per row: the cause is on the page the row opens.
  await expect(panel).not.toContainText("not accepting");
  const block = panel.locator("ul").first().locator("..");
  const look = await block.evaluate((node) => {
    const probe = document.createElement("div");
    probe.className = "bg-muted";
    document.body.append(probe);
    const muted = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const style = getComputedStyle(node);
    return { fill: style.backgroundColor, muted, border: style.borderTopWidth };
  });
  expect(look.fill).toBe(look.muted);
  expect(look.border).toBe("0px");
  const video = page.getByRole("group", { name: "What to make" }).getByRole("button", { name: /Video/ });
  await expect(video).toContainText("Down");
  await expect(video.locator(".rounded-full.bg-destructive")).toHaveCount(0);
});

test("H10 a member never sees the gateway panel", async ({ isolated }) => {
  const member = await isolated.member();
  await member.page.goto("/");
  await expect(member.page.getByRole("button", { name: /^(Make|Send)$/ })).toBeVisible();
  await expect(member.page.getByRole("region", { name: "Your gateway" })).toHaveCount(0);
});

test.describe("H10 at a phone width the owner sees one row that opens Status", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("the phone row links to Status", async ({ page }) => {
    await page.goto("/");
    const row = page.getByRole("region", { name: "Your gateway" }).getByRole("link", { name: /Open Status/ });
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute("href", /^\/status\/?$/);
  });
});

test("S7 a signed-in viewer at / never shows the sign-in heading while the session loads", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  const session = holdRoute();
  await page.route("**/me/api/session", async (route) => {
    await session.released;
    await route.continue();
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Sign in to/ })).toHaveCount(0);
  session.release();
  await expect(page.getByRole("button", { name: /^(Make|Send)$/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Sign in to/ })).toHaveCount(0);
});

// The sign-in page is another lane's change. These cases state its contract and run when that lane lands.
test.fixme("S1 signed out at / with single sign-on only, one primary control links straight to the provider", async () => {});
test.fixme("S2 signed out at /library/?asset=x returns there after the provider callback", async () => {});
test.fixme("S3 owner password reveals the form in place, with focus on Email", async () => {});
test.fixme("S4 a provider error shows the alert, keeps the button, and never redirects", async () => {});
test.fixme("S6 options and setup start together on first render, with almost no layout shift", async () => {});

const LAYOUTS = [
  { width: 1440, height: 900 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
];
for (const viewport of LAYOUTS) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`H14 the member Home at ${viewport.width} px in ${colorScheme} is accessible, fits the width, and has one primary action`, async ({ isolated }) => {
      const member = await isolated.member();
      const { page } = member;
      await page.setViewportSize(viewport);
      await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
      await page.goto("/");
      await expect(page.getByRole("button", { name: /^(Make|Send)$/ })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await expect(primary(page)).toHaveCount(1);
      const results = await new AxeBuilder({ page }).include("main").withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(results.violations.map((item) => item.id)).toEqual([]);
      if (viewport.width === 390) {
        const heights = await page.locator("main button:visible, main a[href]:visible").evaluateAll((items) =>
          items.map((item) => Math.round(item.getBoundingClientRect().height)),
        );
        expect(Math.min(...heights)).toBeGreaterThanOrEqual(44);
      }
    });
  }
}

// The seeded owner has six capabilities: one down (it keeps its state word) and one off (it is not offered).
for (const viewport of LAYOUTS) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`the chooser's options do not overlap at ${viewport.width} px in ${colorScheme}, and a phone row scrolls with an edge`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
      await seedOwner(page, page.context().request);
      await page.goto("/");
      const group = page.getByRole("group", { name: "What to make" });
      const options = group.getByRole("button");
      await expect(options.first()).toBeVisible();
      await expect(options.nth(1)).toBeVisible();
      await expect(group.getByRole("button", { name: /Video/ })).toContainText("Down");
      await expect(group.getByRole("button", { name: /Sound effects/ })).toHaveCount(0);
      await expect(group).not.toContainText("Off");
      const boxes = await options.evaluateAll((items) => items.map((item) => item.getBoundingClientRect().toJSON()));
      for (let index = 1; index < boxes.length; index += 1) {
        // Each option starts where the one before it ends, or later: no two boxes intersect.
        expect(boxes[index].left).toBeGreaterThanOrEqual(boxes[index - 1].right - 0.5);
      }
      const scrolls = await group.evaluate((row) => row.scrollWidth > row.clientWidth + 1);
      if (viewport.width === 390) {
        expect(scrolls).toBe(true);
        await expect(page.locator("[data-slot='chooser-fade']")).toBeVisible();
        // The row bleeds to the screen's right edge, so a chip is cut by the screen, never by the page's
        // padding; its first chip still starts on the page's left edge, and the page never scrolls sideways.
        const row = (await group.boundingBox())!;
        const composer = (await page.locator("[data-slot='composer']").boundingBox())!;
        expect(Math.round(row.x + row.width)).toBe(390);
        expect(Math.abs(boxes[0].left - composer.x)).toBeLessThanOrEqual(1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
      }
    });
  }
}

test("at 390 px the chooser row snaps by proximity from a 20 px edge, shows a word or nothing at its edge, and fades only while more is off-screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await seedOwner(page, page.context().request);
  await page.goto("/");
  const group = page.getByRole("group", { name: "What to make" });
  const fade = page.locator("[data-slot='chooser-fade']");
  // Chromium serialises the default (proximity) strictness as a bare "x"; "mandatory" would show in the value.
  const css = await group.evaluate((row) => ({ snap: getComputedStyle(row).scrollSnapType, padding: getComputedStyle(row).scrollPaddingInlineStart }));
  expect(css.snap).toMatch(/^x( proximity)?$/);
  expect(css.padding).toBe("20px");
  // At the start the row has chips off to the right: the fade shows over its 24 px right edge.
  await expect(fade).toHaveCount(1);
  expect(Math.round((await fade.boundingBox())!.width)).toBe(24);
  // A chip is either mostly in view with its word, or hidden: an icon alone, or half a word, never shows.
  const chips = () =>
    group.evaluate((row) => {
      const box = row.getBoundingClientRect();
      return Array.from(row.querySelectorAll("button")).map((chip) => {
        const word = chip.querySelector<HTMLElement>("[data-slot='segment-word']")!;
        const rect = word.getBoundingClientRect();
        const visible = Math.max(0, Math.min(rect.right, box.right) - Math.max(rect.left, box.left));
        const icon = chip.querySelector("svg");
        return {
          share: rect.width ? visible / rect.width : 0,
          word: getComputedStyle(word).opacity,
          icon: icon ? getComputedStyle(icon).opacity : "1",
        };
      });
    });
  await expect
    .poll(async () =>
      (await chips()).every((chip) => {
        if (chip.share >= 0.7) return chip.word === "1" && chip.icon === "1";
        if (chip.share <= 0.3) return chip.word === "0" && chip.icon === "0";
        return false;
      }),
    )
    .toBe(true);
  // Scrolled to the end, the last chip's word is wholly in view, and nothing is left to the right.
  await group.evaluate((row) => row.scrollTo({ left: row.scrollWidth }));
  await expect(fade).toHaveCount(0);
  await expect.poll(async () => (await chips()).at(-1)?.share ?? 0).toBeGreaterThanOrEqual(0.99);
  // Wide enough for every chip, the row does not scroll, so it has no fade.
  await page.setViewportSize({ width: 1100, height: 900 });
  await expect(fade).toHaveCount(0);
});

test("at 390 px the phone tab bar never covers the end of Home, with the request history notice showing", async ({ isolated }) => {
  const member = await isolated.member();
  const { page } = member;
  await page.setViewportSize({ width: 390, height: 844 });
  await seedReturningMember(page, member.context.request);
  await seedHistoryNoticeDue(page, member.context.request);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Request history is available." })).toBeVisible();
  // The notice's sentence keeps the width, and its two answers wrap under it.
  const notice = page.getByRole("region", { name: "Request history is available." });
  const sentence = await notice.getByRole("paragraph").boundingBox();
  const turnOn = await notice.getByRole("button", { name: "Turn on" }).boundingBox();
  expect(sentence!.width).toBeGreaterThan(240);
  expect(turnOn!.y).toBeGreaterThanOrEqual(sentence!.y + sentence!.height);
  // One app per line at this width, so three apps never wrap two and one.
  const apps = page.getByRole("region", { name: "Your apps" }).getByRole("listitem");
  await expect(apps).toHaveCount(2);
  const lefts = await apps.evaluateAll((items) => items.map((item) => Math.round(item.getBoundingClientRect().left)));
  expect(new Set(lefts).size).toBe(1);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const last = await apps.last().boundingBox();
  const nav = await page.getByRole("navigation", { name: "Phone navigation" }).boundingBox();
  expect(last && nav).toBeTruthy();
  expect(last!.y + last!.height).toBeLessThanOrEqual(nav!.y);
});

const SHOTS = process.env.HOME_SHOTS_DIR;
test.describe("built pages for the design side-by-side check", () => {
  test.skip(!SHOTS, "set HOME_SHOTS_DIR to save the built pages");
  for (const viewport of LAYOUTS) {
    for (const colorScheme of ["light", "dark"] as const) {
      const suffix = `${viewport.width}${colorScheme === "dark" ? "-dark" : ""}`;
      /** The first screen as a person sees it, then the whole page with the phone tab bar in its place at the end (ban 31). */
      const save = async (page: Page, name: string) => {
        mkdirSync(SHOTS!, { recursive: true });
        await page.evaluate(() => document.fonts.ready);
        // The shell's own checks start a moment after the page; wait for their answer, as a person would see it.
        await expect(page.getByText("Checking", { exact: true })).toHaveCount(0, { timeout: 10_000 }).catch(() => undefined);
        // Every picture loaded and decoded, the lazy ones too, so the whole-page capture has no blanks.
        await page.evaluate(async () => {
          const images = Array.from(document.images);
          for (const image of images) image.loading = "eager";
          await Promise.all(images.map((image) => image.decode().catch(() => undefined)));
        });
        await page.screenshot({ path: path.join(SHOTS!, `${name}-${suffix}-fold.png`) });
        await page.addStyleTag({ content: "nav[aria-label='Phone navigation']{position:static!important}" });
        await page.screenshot({ path: path.join(SHOTS!, `${name}-${suffix}.png`), fullPage: true });
      };
      test(`saves the first-visit member Home at ${suffix}`, async ({ isolated }) => {
        const member = await isolated.member();
        const { page } = member;
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await seedFirstVisitMember(page, member.context.request);
        await page.goto("/");
        await expect(page.getByRole("region", { name: "What you can make here" })).toBeVisible();
        await save(page, "home-member-first");
      });
      test(`saves the returning member Home with work in Recent at ${suffix}`, async ({ isolated }) => {
        const member = await isolated.member();
        const { page } = member;
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await seedReturningMember(page, member.context.request);
        await page.goto("/");
        await expect(page.getByRole("list", { name: "Recent work" })).toBeVisible();
        await save(page, "home-member-returning");
      });
      test(`saves the returning member Home with the request history notice at ${suffix}`, async ({ isolated }) => {
        const member = await isolated.member();
        const { page } = member;
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await seedReturningMember(page, member.context.request);
        await seedHistoryNoticeDue(page, member.context.request);
        await page.goto("/");
        await expect(page.getByRole("heading", { name: "Request history is available." })).toBeVisible();
        await save(page, "home-notice");
      });
      test(`saves the owner Home at ${suffix}`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
        await seedOwner(page, page.context().request);
        await page.goto("/");
        await expect(page.getByRole("region", { name: "Your gateway" })).toBeVisible();
        await save(page, "home-owner");
      });
    }
  }
});
