import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile } from "node:fs/promises";
import path from "node:path";

// Prompt memory against the real gateway on the lived-in household
// (docs/design/PROMPT_MEMORY.md). The critical property: a prompt is shown
// to its maker only. Another member and the owner never see it, in the UI
// or in the API the UI reads, including the details download.

type Asset = {
  asset_id: string;
  owner: "sam" | "robin" | "alex";
  kind: string;
  prompt_kept: boolean;
};
type Manifest = {
  owner_email: string;
  member_email: string;
  restricted_email: string;
  password: string;
  people: Record<string, string>;
  assets: Asset[];
  now: number;
};

let seed: Manifest;
const states = new Map<
  string,
  Awaited<ReturnType<BrowserContext["storageState"]>>
>();

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ baseURL }) => {
  const port = new URL(baseURL!).port;
  seed = JSON.parse(
    await readFile(
      path.resolve(
        __dirname,
        `../test-results/household-manifest-${port}.json`,
      ),
      "utf8",
    ),
  );
});

/** A signed-in context; each person signs in once (sign-in is throttled). */
async function as(browser: Browser, email: string, baseURL: string) {
  if (!states.has(email)) {
    const ctx = await browser.newContext({ baseURL });
    const page = await ctx.newPage();
    await page.goto("/sign-in/");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(seed.password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.waitForURL((url) => !url.pathname.includes("/sign-in"));
    states.set(email, await ctx.storageState());
    await ctx.close();
  }
  const ctx = await browser.newContext({
    baseURL,
    storageState: states.get(email),
  });
  return { ctx, page: await ctx.newPage() };
}

const robinsKept = () =>
  seed.assets.find(
    (a) => a.owner === "robin" && a.kind === "image" && a.prompt_kept,
  )!;
const robinsForgotten = () =>
  seed.assets.find((a) => a.owner === "robin" && !a.prompt_kept)!;
const alexsKept = () =>
  seed.assets.find(
    (a) => a.owner === "alex" && a.kind === "image" && a.prompt_kept,
  )!;

async function media(request: APIRequestContext, id: string, everyone = false) {
  const response = await request.get(
    `/me/api/media/${encodeURIComponent(id)}${everyone ? "?scope=everyone" : ""}`,
  );
  return {
    status: response.status(),
    body: response.ok() ? await response.json() : null,
  };
}

// The Library starts every viewer on Mine, so a deep link reaches only the
// viewer's own creations. An owner reaches another person's creation by
// choosing Everyone, then opening its tile.
async function openViewer(page: Page, id: string, everyone = false) {
  if (everyone) {
    await page.goto("/library/");
    await page.getByRole("combobox", { name: "Person", exact: true }).click();
    await page.getByRole("option", { name: "Everyone", exact: true }).click();
    await page.locator(`[data-asset-open="${id}"]`).click();
  } else {
    await page.goto(`/library/?asset=${encodeURIComponent(id)}`);
  }
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // The facts load with the viewer; wait for them before reading its text.
  await expect(dialog.getByText("Model", { exact: true })).toBeVisible();
  return dialog;
}

test.describe("Accessibility of the prompt screens", () => {
  // Axe (serious and critical), no sideways scroll, and 44 px targets on a
  // phone, in both themes: Your settings, the Try row, the maker's viewer.
  for (const width of [390, 1440]) {
    for (const theme of ["light", "dark"] as const) {
      test(`${width} ${theme}`, async ({ browser, baseURL }) => {
        const { ctx, page } = await as(browser, seed.member_email, baseURL!);
        await page.setViewportSize({
          width,
          height: width === 390 ? 844 : 900,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        await page.addInitScript(
          (value) => localStorage.setItem("theme", value),
          theme,
        );
        for (const route of [
          "/account/",
          "/try/?capability=speech",
          `/library/?asset=${robinsKept().asset_id}`,
        ]) {
          await page.goto(route);
          await expect(
            page
              .locator(
                "[data-slot=setting-row], [data-slot=prompt-keep], [role=dialog]",
              )
              .first(),
          ).toBeVisible();
          // Scan the loaded page, not its loading placeholders.
          await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
          const violations = (
            await new AxeBuilder({ page }).analyze()
          ).violations
            .filter((v) => ["serious", "critical"].includes(v.impact ?? ""))
            .map((v) => `${route}: ${v.id}`);
          expect(violations).toEqual([]);
          expect(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
          ).toBe(true);
          if (width === 390) {
            const small = await page
              .locator(
                "[data-slot=setting-row] button, [data-slot=setting-row] [role=switch], [data-slot=setting-row] [role=combobox], [data-slot=prompt-keep] [role=switch]",
              )
              .evaluateAll(
                (nodes) =>
                  nodes
                    .map((node) => node.getBoundingClientRect())
                    .filter(
                      (box) =>
                        box.width > 0 && (box.width < 44 || box.height < 44),
                    ).length,
              );
            expect(small).toBe(0);
          }
        }
        await ctx.close();
      });
    }
  }
});

test.describe("Library: the prompt is the maker's alone", () => {
  test("the maker sees their prompt, Copy prompt and Download with details", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.member_email, baseURL!);
    const item = (await media(page.request, robinsKept().asset_id)).body.asset;
    expect(item.prompt).toBeTruthy();
    expect(item.workflow_download_url).toBeTruthy();
    const dialog = await openViewer(page, robinsKept().asset_id);
    await expect(
      dialog.getByText(
        "Download with details includes prompt and workflow text. Downloaded copies can’t be recalled.",
      ),
    ).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Copy prompt" }),
    ).toBeVisible();
    await dialog.getByRole("button", { name: /^Actions for / }).click();
    await expect(
      page.getByRole("menuitem", { name: "Download with details" }),
    ).toBeVisible();
    const full = await page.request.get(
      `/me/api/media/${robinsKept().asset_id}/download?scope=me&metadata=full`,
    );
    expect(full.status()).toBe(200);
    await ctx.close();
  });

  test("the maker of an unkept prompt reads No saved prompt", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.member_email, baseURL!);
    const item = (await media(page.request, robinsForgotten().asset_id)).body
      .asset;
    expect(item.prompt).toBeNull();
    const dialog = await openViewer(page, robinsForgotten().asset_id);
    await expect(dialog.getByText("No saved prompt")).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Copy prompt" }),
    ).toHaveCount(0);
    await ctx.close();
  });

  test("the owner sees a member's creation with no prompt and no hint of one", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.owner_email, baseURL!);
    const id = robinsKept().asset_id;
    const { body } = await media(page.request, id, true);
    expect(body.asset.prompt).toBeNull();
    expect(body.asset.provenance?.prompt ?? null).toBeNull();
    expect(body.asset.workflow_download_url).toBeNull();
    expect(body.asset.title).toMatch(/^Picture by Robin/);
    const full = await page.request.get(
      `/me/api/media/${id}/download?scope=everyone&metadata=full`,
    );
    expect(full.status()).toBe(403);
    // Prompt search runs over the viewer's own prompts only.
    const robinsWords = "backyard raised vegetable";
    const search = await (
      await page.request.get(
        `/me/api/media?scope=everyone&q=${encodeURIComponent(robinsWords)}`,
      )
    ).json();
    expect(
      search.assets.map((a: { asset_id: string }) => a.asset_id),
    ).not.toContain(id);

    const dialog = await openViewer(page, id, true);
    await expect(dialog).toContainText("Robin");
    await expect(dialog).not.toContainText(/prompt/i);
    await expect(
      dialog.getByRole("button", { name: /^Actions for / }),
    ).toHaveCount(0);
    await ctx.close();
  });

  test("another member cannot reach the item, and keeps their own prompt", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.restricted_email, baseURL!);
    expect((await media(page.request, robinsKept().asset_id)).status).toBe(404);
    const own = (await media(page.request, alexsKept().asset_id)).body.asset;
    expect(own.prompt).toBeTruthy();
    const dialog = await openViewer(page, alexsKept().asset_id);
    await expect(
      dialog.getByRole("button", { name: "Copy prompt" }),
    ).toBeVisible();
    await ctx.close();
  });
});

// Read every Library page: the fixture clock is frozen, so new clips tie on
// created_at with older ones and need not land on the first page.
type AudioItem = { asset_id: string; prompt: string | null };
type AudioPage = { assets: AudioItem[]; next_cursor: string | null };
async function allAudioPages(
  request: import("@playwright/test").APIRequestContext,
  scope: "me" | "everyone",
): Promise<AudioItem[]> {
  const assets: AudioItem[] = [];
  let cursor: string | null = null;
  do {
    const query: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const response = await request.get(
      `/me/api/media?scope=${scope}&kind=audio&limit=100${query}`,
    );
    const body: AudioPage = await response.json();
    assets.push(...body.assets);
    cursor = body.next_cursor;
  } while (cursor);
  return assets;
}

test.describe("Try: Don't keep this prompt", () => {
  // Read text aloud: the household's picture jobs cannot deliver content in
  // this fixture (the job content route has no route policy without a
  // ComfyUI backend in the base config, HTTP 500), and speech is kept the
  // same way: the words with the audio, for the maker only.
  const SPEECH = "/openai/v1/audio/speech";

  test("a kept run is the maker's, a forgotten run keeps nothing", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.member_email, baseURL!);
    const allAudio = () => allAudioPages(page.request, "me");
    const audioIds = async () =>
      new Set<string>((await allAudio()).map((a) => a.asset_id));
    const before = await audioIds();
    await page.goto("/try/?capability=speech");
    const toggle = page.getByRole("switch", { name: "Don’t keep this prompt" });
    await expect(toggle).not.toBeChecked();
    await expect(
      page.getByText("Azizam tries to keep the prompt with the audio."),
    ).toBeVisible();
    await expect(page.getByText("Only you can see kept prompts.")).toBeVisible();

    const kept = `The paper boat is on the puddle ${Date.now()}`;
    await page.getByRole("textbox", { name: "Prompt" }).fill(kept);
    const submit = page.waitForRequest(
      (request) =>
        request.url().endsWith(SPEECH) && request.method() === "POST",
    );
    await page.getByRole("button", { name: "Run" }).click();
    expect((await submit).postDataJSON().extensions).toBeUndefined();
    await expect(
      page.getByRole("heading", { name: "Speech result" }),
    ).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText("Prompt not kept")).toHaveCount(0);

    await toggle.click();
    await expect(
      page.getByText("The audio still goes to Library."),
    ).toBeVisible();
    const forgotten = `A lantern glows in the fog ${Date.now()}`;
    await page.getByRole("textbox", { name: "Prompt" }).fill(forgotten);
    const forgetSubmit = page.waitForRequest(
      (request) =>
        request.url().endsWith(SPEECH) && request.method() === "POST",
    );
    await page.getByRole("button", { name: "Run" }).click();
    expect((await forgetSubmit).postDataJSON().extensions).toEqual({
      "com.azizam.prompts": { mode: "forget" },
    });
    await expect(page.getByText("Prompt not kept")).toBeVisible({
      timeout: 60_000,
    });

    // The maker finds the kept words in Library; the forgotten words are
    // nowhere in what the gateway returns, though both clips are there.
    const mine = { assets: await allAudio() };
    const keptItem = mine.assets.find(
      (a: { prompt: string | null }) => a.prompt === kept,
    );
    expect(keptItem).toBeTruthy();
    expect(JSON.stringify(mine)).not.toContain(forgotten);
    // Two new clips: the kept one, and the forgotten one with no prompt.
    const added = mine.assets.filter(
      (a: { asset_id: string }) => !before.has(a.asset_id),
    );
    expect(added).toHaveLength(2);
    const newest = added.find(
      (a: { asset_id: string }) => a.asset_id !== keptItem!.asset_id,
    );
    expect(newest).toBeTruthy();
    expect(newest!.prompt).toBeNull();
    const dialog = await openViewer(page, newest!.asset_id);
    await expect(dialog.getByText("No saved prompt")).toBeVisible();
    await ctx.close();

    // The owner sees Robin's clips without their words.
    const owner = await as(browser, seed.owner_email, baseURL!);
    const everyone = { assets: await allAudioPages(owner.page.request, "everyone") };
    const ids = everyone.assets.map((a: { asset_id: string }) => a.asset_id);
    expect(ids).toContain(keptItem!.asset_id);
    expect(ids).toContain(newest!.asset_id);
    expect(JSON.stringify(everyone)).not.toContain(kept);
    expect(JSON.stringify(everyone)).not.toContain(forgotten);
    await owner.ctx.close();
  });
});

test.describe("Keys", () => {
  test("a new key on your own App says it acts as you", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.member_email, baseURL!);
    await page.goto("/apps/");
    await page.getByRole("button", { name: "New app" }).first().click();
    await page.getByLabel("App name").fill("Kitchen tablet");
    await page.getByRole("button", { name: "Create app and key" }).click();
    await expect(
      page.getByText(
        "Prompts sent with this key follow your prompt-memory settings. Anyone holding it can access your saved prompts.",
      ),
    ).toBeVisible();
    await ctx.close();
  });

  test("a key the owner makes for someone else's App is not remembered", async ({
    browser,
    baseURL,
  }) => {
    // The web UI has no owner form for this; the gateway's answer is what
    // any such form shows (keyPromptNote, unit-tested).
    const { ctx, page } = await as(browser, seed.owner_email, baseURL!);
    const session = await (await page.request.get("/admin/api/session")).json();
    const apps = await (await page.request.get("/admin/api/apps")).json();
    const robinsApp = apps.apps.find(
      (app: { owner_user_id?: string; kind: string }) =>
        app.kind === "personal" && app.owner_user_id === seed.people.robin,
    );
    const minted = await page.request.post(
      `/admin/api/apps/${robinsApp.id}/keys`,
      {
        data: { label: "Made by Sam" },
        headers: { "x-azizam-csrf": session.actor.csrf_token },
      },
    );
    expect(minted.ok()).toBe(true);
    const { credential } = await minted.json();
    expect(credential.prompt_memory.person_id).toBeNull();
    await ctx.close();
  });
});

test.describe("Your settings", () => {
  test("Delete my saved prompts asks first and keeps the creations", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.restricted_email, baseURL!);
    const before = await (
      await page.request.get("/me/api/media?scope=me&limit=50")
    ).json();
    await page.goto("/");
    await page
      .getByRole("button", { name: /^Account menu/ })
      .first()
      .click();
    await page.getByRole("menuitem", { name: "Your settings" }).click();
    await expect(
      page.getByRole("heading", { name: "Your settings" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Delete…" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(
      "Your pictures, audio and videos stay in Library.",
    );
    await dialog.getByRole("button", { name: "Delete prompts" }).click();
    await expect(
      page.getByText(
        /^Deleted \d+ saved prompts?\.|^Deletion pending\. Azizam will keep retrying\./,
      ),
    ).toBeVisible();
    const after = await (
      await page.request.get("/me/api/media?scope=me&limit=50")
    ).json();
    expect(after.assets.length).toBe(before.assets.length);
    expect(
      after.assets.every((a: { prompt: string | null }) => a.prompt === null),
    ).toBe(true);
    await ctx.close();
  });

  test("turning memory off asks first, and Try then says so", async ({
    browser,
    baseURL,
  }) => {
    const { ctx, page } = await as(browser, seed.restricted_email, baseURL!);
    await page.goto("/account/");
    await page.getByRole("switch", { name: "Remember what I type" }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Turn off" })
      .click();
    await expect(
      page.getByRole("switch", { name: "Remember what I type" }),
    ).not.toBeChecked();
    const memory = await (
      await page.request.get("/me/api/prompt-memory")
    ).json();
    expect(memory.prompt_memory.effective_remember).toBe(false);
    await page.goto("/try/?capability=image");
    await expect(page.getByText("Prompts aren’t kept", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Change" })).toHaveAttribute(
      "href",
      "/account/",
    );
    await page.goto("/account/");
    await page.getByRole("switch", { name: "Remember what I type" }).click();
    await expect(
      page.getByRole("switch", { name: "Remember what I type" }),
    ).toBeChecked();
    await ctx.close();
  });
});

test.describe("Settings: the household's rules", () => {
  test("a cap limits members' choices, and the kill switch overrides them", async ({
    browser,
    baseURL,
  }) => {
    const owner = await as(browser, seed.owner_email, baseURL!);
    await owner.page.goto("/settings/");
    const section = owner.page.locator("section", {
      has: owner.page.getByRole("heading", { name: "Prompts" }),
    });
    await section.getByRole("combobox", { name: "Keep prompts for" }).click();
    await owner.page.getByRole("option", { name: "3 months" }).click();
    await owner.page
      .getByRole("dialog")
      .getByRole("button", { name: "Delete older prompts" })
      .click();
    await expect(
      section.getByRole("combobox", { name: "Keep prompts for" }),
    ).toHaveText("3 months");

    const member = await as(browser, seed.member_email, baseURL!);
    await member.page.goto("/account/");
    await expect(member.page.getByText("Up to 3 months here.")).toBeVisible();
    await member.page
      .getByRole("combobox", { name: "Forget prompts after" })
      .click();
    await expect(
      member.page.getByRole("option", { name: "Never" }),
    ).toHaveAttribute("aria-disabled", "true");
    await member.page.keyboard.press("Escape");

    await section.getByRole("switch", { name: "Never keep prompts" }).click();
    await owner.page
      .getByRole("dialog")
      .getByRole("button", { name: "Never keep prompts" })
      .click();
    await expect(
      section.getByRole("switch", { name: "Never keep prompts" }),
    ).toBeChecked();

    await member.page.reload();
    await expect(
      member.page.getByText("Sam turned this off for everyone."),
    ).toBeVisible();
    const robin = await (
      await member.page.request.get("/me/api/media?scope=me&limit=50")
    ).json();
    expect(
      robin.assets.every((a: { prompt: string | null }) => a.prompt === null),
    ).toBe(true);
    await member.page.goto("/try/?capability=image");
    await expect(
      member.page.getByText("Turned off for everyone on this gateway."),
    ).toBeVisible();

    // Turning it back on asks nothing and restores nothing.
    await section.getByRole("switch", { name: "Never keep prompts" }).click();
    await expect(
      section.getByRole("switch", { name: "Never keep prompts" }),
    ).not.toBeChecked();
    await owner.ctx.close();
    await member.ctx.close();
  });
});

test("Try sends forget for a stale off snapshot even after the backend turns on", async ({
  browser,
  baseURL,
}) => {
  const { ctx, page } = await as(browser, seed.member_email, baseURL!);
  const setMemory = async (remember: boolean) => {
    const session = await (await page.request.get("/me/api/session")).json();
    const saved = await page.request.put("/me/api/prompt-memory", {
      data: { remember, retention: "never" },
      headers: {
        "x-azizam-user-action": "1",
        "x-azizam-csrf": session.csrf_token,
      },
    });
    expect(saved.ok()).toBe(true);
  };
  await setMemory(false);
  await page.goto("/try/?capability=speech");
  await expect(page.getByText("Prompts aren’t kept", { exact: true })).toBeVisible();
  await setMemory(true);
  const text = "stale off snapshot sentinel " + Date.now();
  await page.getByRole("textbox", { name: "Prompt" }).fill(text);
  const submitted = page.waitForRequest(
    (r) => r.url().endsWith("/openai/v1/audio/speech") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Run" }).click();
  expect((await submitted).postDataJSON().extensions).toEqual({
    "com.azizam.prompts": { mode: "forget" },
  });
  await expect(page.getByText("Prompt not kept")).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    page.getByText("The audio still goes to Library."),
  ).toBeVisible();
  const media = await (
    await page.request.get("/me/api/media?scope=me&limit=50")
  ).json();
  expect(JSON.stringify(media)).not.toContain(text);
  await page.goto("/account/");
  await expect(
    page.getByText(
      "Azizam cannot erase an engine’s own history, external output folders, backups, or downloaded copies.",
    ),
  ).toBeVisible();
  await ctx.close();
});
