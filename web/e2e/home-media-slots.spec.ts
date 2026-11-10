import path from "node:path";
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

/*
 * Home's composer builds one labelled slot per entry of a capability row's required_media_inputs, in order. These
 * cases stub the rows the gateway sends for a keyframes video (two pictures), its mid variant (three), a character
 * replacement (a picture, then a video) and an ordinary one-picture edit as the control, and look at the composer
 * at a desktop and a phone width: the empty state, one slot filled, all slots filled. jsdom has no layout, so the
 * checks that the row fits (no sideways scroll, nothing outside the window, wait text whole) are made here.
 * SLOTS_SHOTS_DIR, when set, receives a viewport capture of each state for a person to look at.
 */

type Row = Record<string, unknown>;
type Slot = { name: string; label: string; modality: string; accepts: string[] };

interface Kind {
  kind: string; label: string; title: string; model: string; intent: string;
  inputs: string[]; required: string[]; slots?: Slot[];
}

const START: Slot = { name: "start_image", label: "a starting picture", modality: "image", accepts: ["image/*"] };
const MIDDLE: Slot = { name: "mid_image", label: "a middle picture", modality: "image", accepts: ["image/*"] };
const END: Slot = { name: "end_image", label: "an ending picture", modality: "image", accepts: ["image/*"] };

const KINDS: Kind[] = [
  { kind: "keyframes", label: "Keyframe video", title: "LTX keyframes", model: "ltx-keyframes", intent: "video.generate.keyframes",
    inputs: ["image", "text"], required: ["image", "text"], slots: [START, END] },
  { kind: "keyframes-mid", label: "Keyframe video with a middle", title: "LTX keyframes mid", model: "ltx-keyframes-mid", intent: "video.generate.keyframes-mid",
    inputs: ["image", "text"], required: ["image", "text"], slots: [START, MIDDLE, END] },
  { kind: "scail", label: "Replace a character", title: "SCAIL 2 replace", model: "scail-2-replace", intent: "video.generate.scail-replace",
    inputs: ["image", "video", "text"], required: ["image", "video"],
    slots: [
      { name: "character", label: "a new character", modality: "image", accepts: ["image/*"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: ["video/*"] },
    ] },
  { kind: "edit", label: "Edit a picture", title: "FLUX.2 Klein edit", model: "flux2-klein-edit", intent: "image.edit",
    inputs: ["image", "text"], required: ["image", "text"] },
];
const byKind = (kind: string) => KINDS.find((k) => k.kind === kind)!;

function rowFor(template: Row, o: Kind): Row {
  const outputs = o.kind === "edit" ? ["image"] : ["video"];
  return {
    ...template,
    icon_key: o.kind, capability: o.kind, capability_title: o.label, title: o.title,
    model: o.model, use_name: o.model, recommended_model: o.model, use_names: [o.model],
    summary: "", intent: o.intent, typical_seconds: 10,
    input_modalities: o.inputs, required_input_modalities: o.required, output_modalities: outputs,
    input_words: { image: "picture", video: "video" },
    action_label: "Make", recordable_input_modalities: [],
    cost_hint: { kind: "free", price_usd: null },
    ...(o.slots ? { required_media_inputs: o.slots } : {}),
  };
}

const PICTURE = path.resolve(__dirname, "fixtures/avatar-picture.png");
const VIDEO = path.resolve(__dirname, "fixtures/preview-test.mp4");
const filePath = (slot: Slot) => (slot.modality === "video" ? VIDEO : PICTURE);

/** The words Make is waiting for, as the composer joins them: "a and b", "a, b and c". */
function joined(labels: string[]): string {
  return labels.length < 2 ? labels.join("") : `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}
const waitText = (labels: string[]) => `Add ${joined(labels)} to start`;

async function openKind(page: Page, context: import("@playwright/test").BrowserContext, kind: Kind) {
  const real = await (await context.request.get("/me/api/home")).json();
  const template = real.capabilities.find((r: Row) => r.icon_key === "chat") ?? real.capabilities[0];
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, { ...real, capabilities: KINDS.map((k) => rowFor(template, k)) }),
  );
  await page.goto("/");
  const group = page.getByRole("group", { name: "What to make" });
  await expect(group).toBeVisible();
  await group.getByRole("button", { name: new RegExp("^" + kind.label + " About") }).first().click();
  const composer = page.locator('[data-slot="composer"]');
  await expect(composer).toBeVisible();
  return composer;
}

/** Nothing sideways: the page does not scroll across, and every control of the composer sits inside the window. */
async function expectFits(page: Page, composer: Locator) {
  const page_ = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(page_.scroll, "the page scrolls sideways").toBeLessThanOrEqual(page_.client);
  const width = page.viewportSize()!.width;
  const outside = await composer.locator("button, input:not(.sr-only), img").evaluateAll(
    (nodes, w) => nodes.map((n) => ({ n, r: n.getBoundingClientRect() }))
      .filter(({ r }) => r.width > 0 && (r.left < -0.5 || r.right > w + 0.5))
      .map(({ n, r }) => `${n.tagName} ${n.getAttribute("aria-label") ?? n.textContent} ${Math.round(r.left)}..${Math.round(r.right)}`),
    width,
  );
  expect(outside, "controls outside the window").toEqual([]);
}

/** The class of the icon in a control, to tell a paperclip from a picture or video drawing. */
const iconOf = (control: Locator) => control.locator("svg").first().getAttribute("class");

/**
 * Every pill and chip starts at the prompt text's left edge. At a phone width they also take the composer's inner
 * width, so their right edges are one line too.
 */
async function expectAligned(composer: Locator, fullWidth: boolean) {
  const edges = await composer.evaluate((root) => {
    const pills = Array.from(root.querySelectorAll<HTMLElement>('button[aria-label^="Attach "]'));
    const chips = Array.from(root.querySelectorAll<HTMLElement>('[data-slot="home-picked-file"] > span'));
    const area = root.querySelector<HTMLElement>("textarea");
    const textLeft = area ? area.getBoundingClientRect().left + parseFloat(getComputedStyle(area).paddingLeft) : null;
    return {
      boxes: [...pills, ...chips].map((el) => {
        const r = el.getBoundingClientRect();
        return { what: el.getAttribute("aria-label") ?? el.textContent, left: r.left, right: r.right };
      }),
      textLeft,
    };
  });
  expect(edges.boxes.length).toBeGreaterThan(0);
  const [first] = edges.boxes;
  // Inline pills share a line, so only the first starts at the text's edge; stacked ones all do.
  const startOfLine = fullWidth ? edges.boxes : edges.boxes.filter((box) => box.what?.startsWith("Attach ") !== true || box === first);
  for (const box of startOfLine) {
    if (edges.textLeft !== null) expect(Math.abs(box.left - edges.textLeft), `${box.what} left edge`).toBeLessThanOrEqual(1);
    if (fullWidth) expect(Math.abs(box.right - first.right), `${box.what} right edge`).toBeLessThanOrEqual(1);
  }
}

/** The wait line is whole: its text is not cut by an ellipsis. */
async function expectWaitWhole(page: Page, text: string) {
  const line = page.getByText(text, { exact: true });
  await expect(line).toBeVisible();
  const fit = await line.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  expect(fit.scroll, `wait text is cut off: ${fit.scroll}px of text in ${fit.client}px`).toBeLessThanOrEqual(fit.client);
}

async function shot(page: Page, name: string) {
  const dir = process.env.SLOTS_SHOTS_DIR;
  if (!dir) return;
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  await page.screenshot({ path: path.join(dir, `${name}.png`) });
}

async function seeComposer(composer: Locator) {
  await composer.scrollIntoViewIfNeeded();
}

for (const width of [1440, 390]) {
  for (const kind of KINDS.filter((k) => k.slots)) {
    const slots = kind.slots!;
    const labels = slots.map((s) => s.label);

    test(`${kind.title} offers ${slots.length} labelled slots and names what is missing at ${width}px`, async ({ isolated }) => {
      const { page, context } = await isolated.member();
      await page.setViewportSize({ width, height: 860 });
      const composer = await openKind(page, context, kind);
      await seeComposer(composer);

      // Empty: one distinct, named place for each file, in declaration order; each has its own file input.
      const attach = composer.getByRole("button", { name: /^Attach / });
      await expect(attach).toHaveCount(slots.length);
      for (const [index, slot] of slots.entries()) {
        await expect(attach.nth(index)).toHaveAccessibleName(`Attach ${slot.label}`);
        await expect(composer.getByLabel(`Choose ${slot.label}`, { exact: true })).toHaveCount(1);
      }
      await shot(page, `${kind.kind}-${width}-empty`);
      // Each pill draws its modality's icon, not a paperclip; a video slot reads as a video slot.
      for (const [index, slot] of slots.entries()) {
        const icon = (await iconOf(attach.nth(index))) ?? "";
        expect(icon, `${slot.label} icon`).not.toContain("paperclip");
        if (slot.modality === "video") expect(icon, `${slot.label} icon`).toContain("video");
      }
      await expectAligned(composer, width === 390);
      await expectWaitWhole(page, waitText(labels));
      await expect(page.getByRole("button", { name: "Make", exact: true })).toHaveAttribute("aria-disabled", "true");
      await expectFits(page, composer);

      // Each slot in turn: the filled one is labelled and removable, the wait text names the rest.
      for (const [index, slot] of slots.entries()) {
        await composer.getByLabel(`Choose ${slot.label}`, { exact: true }).setInputFiles(filePath(slot));
        // A filled slot is shown once, as its chip: the chip is the change control, the X removes, no Change pill.
        const chips = composer.locator('[data-slot="home-picked-file"]');
        await expect(chips).toHaveCount(index + 1);
        await expect(composer.getByRole("button", { name: /^Change / })).toHaveCount(index + 1);
        await expect(chips.getByRole("button", { name: /^Change / })).toHaveCount(index + 1);
        await expect(chips.getByRole("button", { name: `Change ${slot.label}` })).toBeVisible();
        await expect(chips.getByRole("button", { name: `Remove ${slot.label}` })).toBeVisible();
        await expect(composer.getByRole("button", { name: /^Remove / })).toHaveCount(index + 1);
        await expect(composer.getByRole("button", { name: `Attach ${slot.label}` })).toHaveCount(0);
        await expectAligned(composer, width === 390);
        const rest = labels.slice(index + 1);
        await shot(page, `${kind.kind}-${width}-${index + 1}-of-${slots.length}`);
        if (rest.length) {
          await expect(composer.getByRole("button", { name: /^Attach / })).toHaveCount(rest.length);
          await expectWaitWhole(page, waitText(rest));
        } else {
          await expect(page.getByText(/^Add .* to start$/)).toHaveCount(0);
        }
        await expectFits(page, composer);
      }

      // All filled: only the words are left (Make waits for them), and the slots keep their labels.
      // A row whose words are optional (the character swap) is ready as soon as its files are.
      const wordsRequired = kind.required.includes("text");
      await expect(page.getByRole("button", { name: "Make", exact: true })).toHaveAttribute("aria-disabled", wordsRequired ? "true" : "false");
      await composer.getByRole("textbox").fill("A slow dolly shot through a quiet harbour");
      await expect(page.getByRole("button", { name: "Make", exact: true })).not.toHaveAttribute("aria-disabled", "true");
      await shot(page, `${kind.kind}-${width}-ready`);
      await expectFits(page, composer);
      await expectAligned(composer, width === 390);
    });
  }

  test(`the one-picture control keeps its old composer at ${width}px`, async ({ isolated }) => {
    const { page, context } = await isolated.member();
    await page.setViewportSize({ width, height: 860 });
    const control = byKind("edit");
    const composer = await openKind(page, context, control);
    await seeComposer(composer);

    // Without the field: one icon-style attach control named by the old word, the old wait line, no labelled chips.
    const attach = composer.getByRole("button", { name: /^Attach / });
    await expect(attach).toHaveCount(1);
    await expect(attach).toHaveAccessibleName("Attach a picture");
    await expect(attach).toHaveText("");
    expect(await iconOf(attach)).toContain("paperclip");
    await expect(composer.locator('[data-slot="home-media-input"]')).toHaveCount(0);
    await shot(page, `edit-${width}-empty`);
    await expectWaitWhole(page, "Add a picture to start");
    await expectFits(page, composer);

    await composer.getByLabel("Choose a picture", { exact: true }).setInputFiles(PICTURE);
    const chip = composer.locator('[data-slot="home-picked-file"]');
    await expect(chip).toHaveCount(1);
    await expect(chip).not.toContainText("a picture:");
    await expect(composer.getByRole("button", { name: "Change picture" })).toBeVisible();
    await expect(composer.getByRole("button", { name: /^Remove / })).toHaveCount(1);
    await expectFits(page, composer);
    await shot(page, `edit-${width}-filled`);
  });
}

test("image and video slot Change and Remove targets are at least 44 px at phone width", async ({ isolated }) => {
  const { page, context } = await isolated.member();
  await page.setViewportSize({ width: 390, height: 860 });
  const kind = byKind("scail");
  const composer = await openKind(page, context, kind);
  await seeComposer(composer);
  for (const slot of kind.slots!) {
    await composer.getByLabel(`Choose ${slot.label}`, { exact: true }).setInputFiles(filePath(slot));
    await expect(composer.getByRole("button", { name: `Change ${slot.label}`, exact: true })).toBeVisible();
  }
  await expect(composer.getByRole("button", { name: /^Change / })).toHaveCount(2);
  await expect(composer.getByRole("button", { name: /^Remove / })).toHaveCount(2);
  for (const slot of kind.slots!) {
    for (const action of ["Change", "Remove"]) {
      const control = composer.getByRole("button", { name: `${action} ${slot.label}`, exact: true });
      const box = await control.boundingBox();
      expect(box, `${action} ${slot.label} has a hit area`).not.toBeNull();
      expect(box!.width, `${action} ${slot.label} width`).toBeGreaterThanOrEqual(44);
      expect(box!.height, `${action} ${slot.label} height`).toBeGreaterThanOrEqual(44);
    }
    // Exercise the added top edge, not just the old 32 px thumbnail's centre.
    const change = composer.getByRole("button", { name: `Change ${slot.label}`, exact: true });
    const box = (await change.boundingBox())!;
    await change.click({ position: { x: box.width / 2, y: 1 } });
    const choose = page.getByRole("menuitem", { name: `Choose ${slot.label}`, exact: true });
    await expect(choose).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Choose from Library", exact: true })).toBeVisible();
    const [picker] = await Promise.all([
      page.waitForEvent("filechooser"),
      choose.click(),
    ]);
    const [pickerLabel, pickerAccept] = await picker
      .element()
      .evaluate((el) => [(el as HTMLInputElement).ariaLabel, (el as HTMLInputElement).accept]);
    expect(pickerLabel).toBe(`Choose ${slot.label}`);
    expect(pickerAccept).toBe(slot.accepts.join(","));
    await picker.setFiles(filePath(slot));
    await expect(choose).toBeHidden();
    await expect(change).toBeVisible();
  }
  await expectFits(page, composer);
});
