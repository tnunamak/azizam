import { execFileSync } from "node:child_process";
import path from "node:path";
import type { Locator, Page } from "@playwright/test";
import { expect, fixtureStateDir, GLOBAL_STATE_TAG, test } from "./fixtures/isolation";

// The journey a member takes through request history, at a desktop and a phone width: the owner is a person
// too, so the owner's own switch, Try, History and the Home notice are the real ones. It runs on the serial
// global-state gateway, because it changes the owner's history choice, and it puts that choice back.

const shot = (width: number, name: string) =>
  path.resolve(__dirname, "../test-results/screens", `history-5b1-${width}-${name}.png`);

/**
 * A whole-page capture. The phone tab bar is fixed to the screen's bottom, so a full-page capture would
 * paint it over the middle of the page; for the capture it sits in its place at the end (ban 31).
 */
async function saveWholePage(page: Page, file: string) {
  const release = await page.addStyleTag({ content: "nav[aria-label='Phone navigation']{position:static!important}" });
  await page.screenshot({ path: file, fullPage: true });
  await release.evaluate((node) => (node as Element).remove());
}

// The Home notice needs a member who predates request history: the row the schema 17 upgrade wrote, with the
// switch off and no last writer (retention_migration.migrate_group_keys), and no answer to the notice yet. Make the
// owner that member again, so each width starts from the same state.
function makePredatingMember(baseURL: string) {
  const admin = path.join(fixtureStateDir(Number(new URL(baseURL).port)), "admin.db");
  const owner = "SELECT id FROM users WHERE email='owner@example.test'";
  execFileSync("sqlite3", [
    "-cmd",
    ".timeout 10000",
    admin,
    "UPDATE prompt_memory_preferences SET remember=0, updated_by=NULL WHERE grp='request_history' " +
      `AND user_id IN (${owner}); ` +
      `DELETE FROM request_history_notice_seen WHERE person_id IN (${owner});`,
  ]);
}

// The drawer is on screen and nothing in it is clipped: it waits for the slide-in to end, then checks the drawer's
// edges, every element's right edge against the drawer's, and that the page itself does not scroll sideways.
async function expectDrawerFits(page: Page, drawer: Locator, width: number) {
  await expect.poll(() => drawer.evaluate((el) => el.getAnimations().length)).toBe(0);
  const box = await drawer.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  expect(await drawer.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
  const clipped = await drawer.evaluate((el) => {
    const edge = el.getBoundingClientRect().right;
    return Array.from(el.querySelectorAll("*"))
      .filter((node) => node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().right > edge + 1)
      .map((node) => `${node.tagName.toLowerCase()}: ${(node.textContent ?? "").trim().slice(0, 40)}`);
  });
  expect(clipped).toEqual([]);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth),
  ).toBeLessThanOrEqual(0);
}

for (const width of [1440, 390]) {
  test(`a request is kept in History, read as a conversation, opened and deleted, and a request with keep request history off is not kept, at ${width}px`, { tag: GLOBAL_STATE_TAG }, async ({
    page,
    baseURL,
  }) => {
    await page.setViewportSize({ width, height: 900 });

    // Your settings: history is on for a new person. Turning it off asks first; turning it on needs no confirm.
    await page.goto("/account/");
    await expect(page.getByRole("heading", { name: "Request history" })).toBeVisible();
    const keep = page.getByRole("switch", { name: "Keep request history" });
    await expect(keep).toBeChecked();
    await keep.click();
    const confirm = page.getByRole("dialog");
    await expect(confirm.getByText("Stop keeping request history?")).toBeVisible();
    await confirm.getByRole("button", { name: "Turn off" }).click();
    await expect(keep).not.toBeChecked();
    await expect(page.getByText("Nothing your apps send is kept.")).toBeVisible();
    await keep.click();
    await expect(keep).toBeChecked();
    await expect(page.getByText("Azizam keeps what your apps send so you can look back at it.")).toBeVisible();

    // History: count the rows before the journey adds one.
    await page.goto("/history/");
    const rows = page.getByRole("list", { name: "Kept requests" }).getByRole("listitem");
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    const before = await rows.count();

    // Try: a chat request, with keep request history on (the default), is kept.
    const kept = `History journey ${width} ${Date.now()}`;
    await page.goto("/try/?capability=chat");
    await expect(page.getByRole("switch", { name: "Keep request history" })).toBeChecked();
    await page.getByRole("textbox", { name: "Prompt" }).fill(kept);
    await page.getByRole("button", { name: "Run" }).click();
    await expect(page.getByRole("heading", { name: "Chat result" })).toBeVisible({ timeout: 60_000 });

    // History: the request is the newest row, and its second line is what was asked, not the route.
    await page.goto("/history/");
    await expect(rows).toHaveCount(before + 1, { timeout: 30_000 });
    // The model's name settles once the catalog has loaded; until then the row may show a capability's name.
    await expect(rows.first()).toContainText("Fast");
    await expect(rows.first()).toContainText(kept);
    await expect(rows.first()).not.toContainText("/chat/completions");
    await saveWholePage(page, shot(width, "list"));

    // Open it: what was asked and the reply are chat bubbles, the receipt's sentence says what was kept, and the raw
    // record is folded away at the bottom.
    await rows.first().getByRole("button").click();
    const detail = page.getByRole("dialog");
    await expect(detail.getByRole("heading", { name: "Fast" })).toBeVisible();
    const conversation = detail.getByRole("list", { name: "Conversation" });
    await expect(conversation.getByText(kept, { exact: true })).toBeVisible();
    await expect(detail.getByText(/^Kept the request and its reply until /)).toBeVisible({ timeout: 15_000 });
    await expect(detail.getByText("What was sent")).toHaveCount(0);
    const technical = detail.locator("details", { hasText: "Technical details" });
    await expect(technical).not.toHaveAttribute("open");
    await expectDrawerFits(page, detail, width);
    await saveWholePage(page, shot(width, "detail"));

    // Delete it: after the confirm the list is back where it was.
    await detail.getByRole("button", { name: "Delete this request" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Delete request" }).click();
    await expect(rows).toHaveCount(before, { timeout: 15_000 });
    await expect(page.getByText(kept)).toHaveCount(0);

    // Keep request history off: the same request is not kept, and the result says so.
    const hidden = `Keep history off journey ${width} ${Date.now()}`;
    await page.goto("/try/?capability=chat");
    await page.getByRole("switch", { name: "Keep request history" }).click();
    await expect(page.getByText("Not saved in your History.", { exact: true })).toBeVisible();
    await saveWholePage(page, shot(width, "try-keep-history-off"));
    await page.getByRole("textbox", { name: "Prompt" }).fill(hidden);
    await page.getByRole("button", { name: "Run" }).click();
    await expect(page.getByRole("heading", { name: "Chat result" })).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("Not saved in your History", { exact: true })).toBeVisible();
    await page.goto("/history/");
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    await expect(rows).toHaveCount(before);
    await expect(page.getByText(hidden)).toHaveCount(0);

    // Home: a member who predates request history is told once. "Not now" and "Turn on" both answer it.
    makePredatingMember(baseURL!);
    await page.goto("/");
    const notice = page.getByText("Request history is available.");
    await expect(notice).toBeVisible();
    await expect(page.getByText("Look back at what you asked. It’s off for you until you turn it on.")).toBeVisible();
    await saveWholePage(page, shot(width, "home-notice"));
    if (width === 390) {
      await page.getByRole("button", { name: "Turn on" }).click();
    } else {
      await page.getByRole("button", { name: "Not now" }).click();
    }
    await expect(notice).toHaveCount(0);
    await page.reload();
    await expect(notice).toHaveCount(0);

    // Put the owner's choice back on, so the next run starts as a new person does.
    await page.goto("/account/");
    const again = page.getByRole("switch", { name: "Keep request history" });
    if (!(await again.isChecked())) await again.click();
    await expect(again).toBeChecked();
    await page.goto("/account/");
    await saveWholePage(page, shot(width, "settings"));
  });
}
