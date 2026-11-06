import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";

/*
 * The model chip on Home keeps the whole model name on a desktop-width window. jsdom has no layout, so this is the
 * check that fails when the name is cut off: the chip's text must fit its box (scrollWidth within clientWidth), not
 * end in an ellipsis. The rows are the shapes the gateway sends for a workflow with a file input.
 */

type Row = Record<string, unknown>;

const KINDS = [
  { kind: "mesh", label: "3D models", title: "TRELLIS.2 image to 3D", model: "mesh", intent: "mesh.generate", inputs: ["image"], required: ["image"], outputs: ["model"], action: "Make" },
  { kind: "edit", label: "Edit a picture", title: "FLUX.2 Klein edit", model: "flux2-klein-edit", intent: "image.edit", inputs: ["image", "text"], required: ["image", "text"], outputs: ["image"], action: "Make" },
  { kind: "transcribe", label: "Transcription", title: "Whisper", model: "whisper-1", intent: "audio.transcriptions", inputs: ["audio"], required: ["audio"], outputs: ["text"], action: "Transcribe" },
];

function rowFor(template: Row, o: (typeof KINDS)[number]): Row {
  return {
    ...template,
    icon_key: o.kind, capability: o.kind, capability_title: o.label, title: o.title,
    model: o.model, use_name: o.model, recommended_model: o.model, use_names: [o.model],
    summary: "", intent: o.intent, typical_seconds: 10,
    input_modalities: o.inputs, required_input_modalities: o.required, output_modalities: o.outputs,
    input_words: { image: "picture", audio: "recording" },
    action_label: o.action, recordable_input_modalities: [],
    cost_hint: { kind: "free", price_usd: null },
  };
}

for (const width of [1280, 1024]) {
  for (const o of KINDS) {
    test(`the model chip shows the whole name of ${o.title} at ${width}px`, async ({ isolated }) => {
      const { page, context } = await isolated.member();
      await page.setViewportSize({ width, height: 860 });
      const real = await (await context.request.get("/me/api/home")).json();
      const template = real.capabilities.find((r: Row) => r.icon_key === "chat") ?? real.capabilities[0];
      await page.route("**/me/api/home", (route) =>
        fulfilJson(route, { ...real, capabilities: KINDS.map((k) => rowFor(template, k)) }),
      );
      await page.goto("/");
      const group = page.getByRole("group", { name: "What to make" });
      await expect(group).toBeVisible();
      await group.getByRole("button", { name: new RegExp("^" + o.label) }).first().click();
      const chip = page.getByRole("button", { name: o.title, exact: true });
      await expect(chip).toBeVisible();
      const fit = await chip.locator("span.truncate").evaluate((el) => ({
        scroll: el.scrollWidth, client: el.clientWidth, text: el.textContent,
      }));
      expect(fit.text).toBe(o.title);
      expect(fit.scroll, `${o.title} is cut off: ${fit.scroll}px of text in ${fit.client}px`).toBeLessThanOrEqual(fit.client);
    });
  }
}
