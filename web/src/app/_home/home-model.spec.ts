import { describe, expect, it } from "vitest";
import type { HomeCapability, HomeChat, HomeCreation, HomeReceipt } from "./home-api";
import {
  capabilityIs,
  chooserRows,
  composerInputs,
  firstNameOf,
  fitsMediaInput,
  mediaPhrase,
  formatWait,
  gatewayNeeds,
  greetingFor,
  greetingParts,
  kindOf,
  listFacts,
  wordsToMediaRow,
  makingPrompt,
  makingRisk,
  makingTimeLeft,
  offeredRows,
  orderChooser,
  promptPlaceholder,
  recentItems,
  runFacts,
  workingLine,
} from "./home-model";
import type { CapabilityView } from "@/lib/capability-state";

/** A capability row as `/me/api/home` sends it; only the fields Home reads are set. */
function row(overrides: Partial<HomeCapability> & { icon_key: string }): HomeCapability {
  return {
    capability: overrides.icon_key,
    capability_title: overrides.icon_key,
    title: overrides.title ?? "Model",
    use_name: overrides.use_name ?? "model",
    use_names: overrides.use_names ?? ["model"],
    intent: "x",
    status: "ready",
    reason_code: "ready",
    available: true,
    typical_seconds: null,
    cost_hint: { kind: "free", price_usd: null },
    summary: "",
    ...overrides,
  } as unknown as HomeCapability;
}

const NOW = 1_800_000_000;

describe("H4 facts under the composer", () => {
  it("reads a local model as no charge with its time", () => {
    expect(runFacts(row({ icon_key: "image", typical_seconds: 9 }))).toBe("No charge · about 9 s");
  });

  it("reads a long wait in minutes", () => {
    expect(formatWait(150)).toBe("about 3 min");
    expect(runFacts(row({ icon_key: "video", typical_seconds: 150 }))).toBe("No charge · about 3 min");
  });

  it("shows a paid model, and the amount only when the data gives one", () => {
    const paid = { kind: "paid" as const, price_usd: 0.04 };
    expect(runFacts(row({ icon_key: "image", typical_seconds: 20, cost_hint: paid }))).toBe(
      "Paid model · about USD 0.04 · about 20 s",
    );
    expect(
      runFacts(row({ icon_key: "image", typical_seconds: 20, cost_hint: { kind: "paid", price_usd: null } })),
    ).toBe("Paid model · about 20 s");
  });

  it("omits a missing time and never says unknown", () => {
    const facts = runFacts(row({ icon_key: "image", typical_seconds: null, cost_hint: { kind: "unknown", price_usd: null } }));
    expect(facts).toBe("");
    expect(facts.toLowerCase()).not.toContain("unknown");
  });

  it("says a chat starts in about a second", () => {
    expect(runFacts(row({ icon_key: "chat", typical_seconds: 1 }))).toBe("No charge · starts in about 1 s");
  });

  it("lists the wait first, in lower case for the cost", () => {
    expect(listFacts(row({ icon_key: "voice" as never, typical_seconds: 3 }))).toBe("About 3 s · no charge");
    expect(listFacts(row({ icon_key: "chat", typical_seconds: 1 }))).toBe("About 1 s · no charge");
  });
});

describe("H2 chooser rows come from the rows, in the person's order", () => {
  it("drops the app plumbing kinds from the chooser", () => {
    const rows = [row({ icon_key: "image" }), row({ icon_key: "embed" }), row({ icon_key: "decide" })];
    expect(chooserRows(rows).map(kindOf)).toEqual(["image"]);
  });

  it("puts the kind the person uses most, then the last used, ahead of catalog order", () => {
    const rows = [row({ icon_key: "chat" }), row({ icon_key: "image" }), row({ icon_key: "video" })];
    const receipts = [
      { request_id: "a", intent: "video.generate", path: "/gateway/v1/media/jobs", status: "succeeded", accepted_at_ms: 5_000 },
      { request_id: "b", intent: "video.generate", path: "/gateway/v1/media/jobs", status: "succeeded", accepted_at_ms: 6_000 },
      { request_id: "c", intent: "image.generate", path: "/v1/images", status: "succeeded", accepted_at_ms: 7_000 },
    ] as unknown as HomeReceipt[];
    expect(orderChooser(rows, receipts).map(kindOf)).toEqual(["video", "image", "chat"]);
  });

  it("keeps the gateway's order when nothing was used", () => {
    const rows = [row({ icon_key: "chat" }), row({ icon_key: "image" })];
    expect(orderChooser(rows, []).map(kindOf)).toEqual(["chat", "image"]);
  });
});

describe("H6 Recent is the person's own work, makes first", () => {
  const media = [
    { asset_id: "m1", kind: "image", created_at: NOW - 3600, prompt: "Garden" },
    { asset_id: "m2", kind: "video", created_at: NOW - 60, prompt: "Clip", available: false },
  ] as unknown as HomeCreation[];
  const chats: HomeChat[] = [{ request_id: "r1", record_id: "c1", created_at: NOW - 7200, title: "Plan a trip" }];
  const receipts = [
    { request_id: "j1", intent: "image.generate", path: "/gateway/v1/media/jobs", status: "running", accepted_at_ms: (NOW - 4) * 1000 },
    { request_id: "j2", intent: "chat.completions", path: "/v1/chat/completions", status: "running", accepted_at_ms: (NOW - 1) * 1000 },
  ] as unknown as HomeReceipt[];
  const rows = [row({ icon_key: "image", typical_seconds: 9 })];

  it("puts in-progress makes first, then results newest first, without unavailable media", () => {
    const items = recentItems({ receipts, media, chats, rows });
    expect(items.map((item) => item.key)).toEqual(["making:j1", "media:m1", "chat:c1"]);
  });

  it("shows a make in progress with the time left, and reads Taking longer than usual past the typical time", () => {
    const [making] = recentItems({ receipts, media, chats, rows });
    if (making.kind !== "making") throw new Error("expected a making item");
    expect(makingTimeLeft(making, NOW)).toBe("about 5 s left");
    expect(makingTimeLeft(making, NOW + 60)).toBe("Taking longer than usual");
  });

  it("never lists a chat that request history did not keep (no title means no row)", () => {
    expect(recentItems({ receipts: [], media: [], chats: [], rows })).toEqual([]);
  });
});

/** A capability view as the owner's panel reads it; only the fields the needs read are set. */
function view(key: string, label: string, state: CapabilityView["state"], word: string, detail: string | null = null): CapabilityView {
  return { key, label, state, word, tone: "ok", detail, checkedAt: null, usedAt: null, failing: [] };
}

const NO_FAULTS = { engineFault: null, providerFault: null, noEngine: false, capabilities: [] };

describe("H9 the owner's panel names operational facts only", () => {
  it("gives each need one place to fix it, and only the needs that exist", () => {
    const needs = gatewayNeeds({ ...NO_FAULTS, engineFault: "Video studio stopped answering." });
    expect(needs).toEqual([
      {
        key: "engine",
        tone: "down",
        fact: "Video studio stopped answering.",
        action: { label: "Open Engines", href: "/engines/" },
      },
    ]);
    expect(gatewayNeeds(NO_FAULTS)).toEqual([]);
  });

  it("sends a gateway with no engine to Engines", () => {
    const [need] = gatewayNeeds({ ...NO_FAULTS, noEngine: true });
    expect(need.action.href).toBe("/engines/");
  });

  it("names each fault, and every capability that is off in one row", () => {
    const needs = gatewayNeeds({
      ...NO_FAULTS,
      capabilities: [
        view("chat", "Chat", "ready", "Ready"),
        view("video", "Video", "down", "Down", "Engine can’t take requests right now."),
        view("image", "Images", "off", "Off", "Not set up yet."),
        view("speech", "Voice", "degraded", "Degraded"),
        view("sfx", "Sound effects", "off", "Off", "Turned off by the owner."),
      ],
    });
    expect(needs).toEqual([
      {
        key: "capability:video",
        tone: "down",
        fact: "Video is down",
        action: { label: "Open Engines", href: "/engines/" },
      },
      {
        key: "capability:speech",
        tone: "degraded",
        fact: "Voice is degraded",
        action: { label: "Open Engines", href: "/engines/" },
      },
      {
        key: "capability:off",
        tone: "off",
        fact: "Images and Sound effects are off",
        action: { label: "Open Engines", href: "/engines/" },
      },
    ]);
    // The owner turned these off or has not set them up: the panel never tells the owner what the owner did.
    expect(JSON.stringify(needs)).not.toMatch(/Turned off by the owner|Not set up yet/);
  });

  it("shows an engine fault once when a capability row already names the failing engine", () => {
    const down = { ...view("video", "Video", "down", "Down", "Video studio is not accepting connections."), failing: [{}] } as CapabilityView;
    const needs = gatewayNeeds({ ...NO_FAULTS, engineFault: "Video studio is unavailable.", capabilities: [down] });
    expect(needs.map((need) => need.key)).toEqual(["capability:video"]);
  });

  it("takes a plural verb for a plural label", () => {
    expect(capabilityIs("Images", "off")).toBe("Images are off");
    expect(capabilityIs("Sound effects", "down")).toBe("Sound effects are down");
    expect(capabilityIs("Video", "down")).toBe("Video is down");
  });

  it("never lists a capability that works, or one whose state is unknown", () => {
    const needs = gatewayNeeds({
      ...NO_FAULTS,
      capabilities: [view("chat", "Chat", "ready", "Ready"), view("speech", "Voice", "unknown", "Unknown")],
    });
    expect(needs).toEqual([]);
  });

  it("counts what works against what is on, in one line", () => {
    expect(workingLine([view("chat", "Chat", "ready", "Ready")])).toBe("1 of 1 working");
    expect(workingLine([view("image", "Images", "ready", "Ready"), view("chat", "Chat", "ready", "Ready")])).toBe("2 of 2 working");
    expect(
      workingLine([
        view("chat", "Chat", "ready", "Ready"),
        view("image", "Images", "busy", "Busy"),
        view("speech", "Voice", "ready", "Ready"),
        view("video", "Video", "down", "Down"),
      ]),
    ).toBe("3 of 4 working");
    // Off is not on, so it is not counted; a fault is on, so it is.
    expect(
      workingLine([view("chat", "Chat", "ready", "Ready"), view("sfx", "Sound effects", "off", "Off"), view("video", "Video", "down", "Down")]),
    ).toBe("1 of 2 working");
    expect(workingLine([view("video", "Video", "down", "Down")])).toBeNull();
  });
});

describe("the chooser offers only what the person can use", () => {
  it("leaves out off and blocked capabilities, and keeps one that is down so its fault can be explained", () => {
    const rows = [row({ icon_key: "chat" }), row({ icon_key: "image" }), row({ icon_key: "video" }), row({ icon_key: "music" })];
    const views = [
      view("chat", "Chat", "ready", "Ready"),
      view("image", "Images", "off", "Off"),
      view("video", "Video", "down", "Down"),
      view("music", "Music", "blocked", "Blocked"),
    ];
    expect(offeredRows(rows, views).map(kindOf)).toEqual(["chat", "video"]);
  });

  it("offers a row no view knows yet", () => {
    expect(offeredRows([row({ icon_key: "chat" })], []).map(kindOf)).toEqual(["chat"]);
  });
});

describe("greeting and names", () => {
  it("greets by time of day and welcomes a first visit", () => {
    expect(greetingFor(new Date(2026, 9, 23, 19), "Robin", false)).toBe("Good evening, Robin");
    expect(greetingFor(new Date(2026, 9, 23, 9), null, false)).toBe("Good morning");
    expect(greetingFor(new Date(2026, 9, 23, 13), "June", true)).toBe("Welcome, June");
  });

  it("splits the greeting so the name can be set quieter than the words before it", () => {
    expect(greetingParts(new Date(2026, 9, 23, 13), "Robin", false)).toEqual({ lead: "Good afternoon", name: "Robin" });
    expect(greetingParts(new Date(2026, 9, 23, 13), null, true)).toEqual({ lead: "Welcome", name: null });
  });

  it("uses the first word of a name", () => {
    expect(firstNameOf("Robin Okafor")).toBe("Robin");
    expect(firstNameOf("  ")).toBeNull();
    expect(firstNameOf("robin@example.test")).toBeNull();
  });
});

describe("H13 no model or engine names in the Home code", () => {
  it("gives the placeholder the capability's manifest declares, with a plain default", () => {
    expect(promptPlaceholder({ prompt_hint: "Describe the picture you want" } as HomeCapability)).toBe(
      "Describe the picture you want",
    );
    expect(promptPlaceholder({ prompt_hint: null } as HomeCapability)).toBe("Describe what you want");
    expect(promptPlaceholder({ prompt_hint: "  " } as HomeCapability)).toBe("Describe what you want");
    expect(promptPlaceholder(null)).toBe("Describe what you want");
  });
});

describe("H4 the facts line shows only the parts that are known", () => {
  const unknownCost = { kind: "unknown", price_usd: null } as const;

  it("says no charge when only the cost is known", () => {
    expect(runFacts(row({ icon_key: "image", typical_seconds: null, cost_hint: { kind: "free", price_usd: null } }))).toBe(
      "No charge",
    );
  });

  it("says the wait when only the wait is known", () => {
    expect(runFacts(row({ icon_key: "image", typical_seconds: 9, cost_hint: unknownCost }))).toBe("About 9 s");
  });

  it("says a chat starts in its wait when only that is known", () => {
    expect(runFacts(row({ icon_key: "chat", typical_seconds: 1, cost_hint: unknownCost }))).toBe("Starts in about 1 s");
  });

  it("is empty when neither is known, so the composer shows no line", () => {
    expect(runFacts(row({ icon_key: "image", typical_seconds: null, cost_hint: unknownCost }))).toBe("");
  });

  it("names a paid model without an amount the data does not give", () => {
    expect(runFacts(row({ icon_key: "image", typical_seconds: null, cost_hint: { kind: "paid", price_usd: null } }))).toBe(
      "Paid model",
    );
  });
});

describe("a make on a capability that is not working says so", () => {
  it("warns that a running make may not finish, and only when its capability is down", () => {
    const item = { kind: "making" as const, key: "making:j", making: "image" as const, startedAt: NOW - 3, typicalSeconds: 9, capability: "image" };
    const view = (state: string, word: string) => ({ key: "image", label: "Images", state, word }) as never;
    expect(makingRisk(item, [view("down", "Down")])).toBe("Images down: it may not finish");
    expect(makingRisk(item, [view("ready", "Ready")])).toBeNull();
    expect(makingRisk(item, [])).toBeNull();
  });
});

describe("a running make shows the words it was started with from this tab", () => {
  const item = { kind: "making" as const, key: "making:j", making: "image" as const, startedAt: NOW - 3, typicalSeconds: 9, capability: "image" };
  it("takes the newest Make of the same capability pressed shortly before the run was accepted", () => {
    const made = [
      { capability: "image", prompt: "A shed in spring rain", at: NOW - 5 },
      { capability: "image", prompt: "An older picture", at: NOW - 900 },
    ];
    expect(makingPrompt(item, made)).toBe("A shed in spring rain");
  });
  it("shows no words for another capability, a Make long before the run, or a run of unknown start", () => {
    expect(makingPrompt(item, [{ capability: "speech", prompt: "A story", at: NOW - 5 }])).toBeNull();
    expect(makingPrompt(item, [{ capability: "image", prompt: "Too old", at: NOW - 900 }])).toBeNull();
    expect(makingPrompt({ ...item, startedAt: null }, [{ capability: "image", prompt: "x", at: NOW - 5 }])).toBeNull();
  });
});

describe("composerInputs reads what a capability takes from its declared modalities", () => {
  const WORDS: Record<string, string> = { image: "picture", audio: "recording", video: "clip" };
  const row = (input: string[], required?: string[]) =>
    ({
      input_modalities: input,
      input_words: Object.fromEntries(input.filter((modality) => modality in WORDS).map((modality) => [modality, WORDS[modality]])),
      ...(required ? { required_input_modalities: required } : {}),
    }) as unknown as HomeCapability;

  it("asks for text when the data declares nothing", () => {
    expect(composerInputs(row([]))).toEqual({ text: "required", media: [] });
    expect(composerInputs(null)).toEqual({ text: "required", media: [] });
  });

  it("text only", () => {
    expect(composerInputs(row(["text"]))).toEqual({ text: "required", media: [] });
  });

  it("image only: no text, one required picture", () => {
    expect(composerInputs(row(["image"], ["image"]))).toEqual({
      text: "none",
      media: [{ modality: "image", word: "picture", accept: "image/*", required: true }],
    });
  });

  it("image plus text, both required", () => {
    const inputs = composerInputs(row(["text", "image"], ["text", "image"]));
    expect(inputs.text).toBe("required");
    expect(inputs.media.map((media) => media.modality)).toEqual(["image"]);
  });

  it("image required, text optional", () => {
    expect(composerInputs(row(["text", "image"], ["image"])).text).toBe("optional");
  });

  it("a file that only helps (a vision chat model's picture) is left to Try", () => {
    expect(composerInputs(row(["text", "image"], ["text"]))).toEqual({ text: "required", media: [] });
    expect(composerInputs(row(["text", "image"]))).toEqual({ text: "required", media: [] });
  });

  it("takes the word from the row, not from a table in the code", () => {
    const odd = { input_modalities: ["image"], input_words: { image: "photo" } } as unknown as HomeCapability;
    expect(composerInputs(odd).media[0]).toMatchObject({ word: "photo", accept: "image/*" });
    const bare = { input_modalities: ["image"] } as unknown as HomeCapability;
    expect(composerInputs(bare).media[0]).toMatchObject({ word: "image", accept: "image/*" });
  });

  it("audio, and a modality no code knows (no accept filter, its own name as the word)", () => {
    expect(composerInputs(row(["audio"])).media[0]).toMatchObject({ word: "recording", accept: "audio/*" });
    expect(composerInputs(row(["chemical"])).media[0]).toMatchObject({ word: "chemical", accept: undefined, required: true });
  });
});

describe("wordsToMediaRow finds a capability that makes the modality from words alone", () => {
  const make = (name: string, input: string[], output: string[], required?: string[]) =>
    ({ capability: name, input_modalities: input, output_modalities: output, required_input_modalities: required }) as unknown as HomeCapability;

  it("picks the text-to-image row, not one that needs a picture or makes something else", () => {
    const edit = make("edit", ["text", "image"], ["image"], ["text", "image"]);
    const mesh = make("mesh", ["image"], ["mesh"]);
    const draw = make("draw", ["text"], ["image"]);
    expect(wordsToMediaRow([mesh, edit, draw], "image")).toBe(draw);
    expect(wordsToMediaRow([mesh, edit], "image")).toBeNull();
  });
});

describe("composerInputs keeps named required media slots", () => {
  const keyframes = [
    { name: "image", label: "a starting picture", modality: "image", accepts: ["image/png", "image/jpeg"] },
    { name: "last_frame", label: "an ending picture", modality: "image", accepts: ["image/*"] },
  ];
  const declared = (required_media_inputs = keyframes) => row({
    icon_key: "video", input_modalities: ["text", "image"], required_input_modalities: ["image"],
    input_words: { image: "picture", video: "clip" }, required_media_inputs,
  });

  it("keyframes has two separately named picture slots, in declaration order", () => {
    const inputs = composerInputs(declared());
    expect(inputs.text).toBe("optional");
    expect(inputs.media).toEqual([
      { name: "image", label: "a starting picture", modality: "image", word: "picture", accept: "image/png,image/jpeg", required: true },
      { name: "last_frame", label: "an ending picture", modality: "image", word: "picture", accept: "image/*", required: true },
    ]);
    expect(inputs.media.map(mediaPhrase)).toEqual(["a starting picture", "an ending picture"]);
    expect(fitsMediaInput(new File(["p"], "p.jpg", { type: "image/jpeg" }), inputs.media[0])).toBe(true);
    expect(fitsMediaInput(new File(["p"], "p.gif", { type: "image/gif" }), inputs.media[0])).toBe(false);
  });

  it("mid keeps a third picture and scail keeps picture then video, with each input's filter", () => {
    const mid = [...keyframes, { name: "mid_frame", label: "a middle picture", modality: "image", accepts: ["image/png"] }];
    expect(composerInputs(declared(mid)).media.map((input) => input.name)).toEqual(["image", "last_frame", "mid_frame"]);
    const scail = [
      { name: "reference_image", label: "a new character", modality: "image", accepts: ["image/png"] },
      { name: "source_video", label: "a video to change", modality: "video", accepts: ["video/mp4"] },
    ];
    expect(composerInputs(declared(scail)).media).toEqual([
      { name: "reference_image", label: "a new character", modality: "image", word: "picture", accept: "image/png", required: true },
      { name: "source_video", label: "a video to change", modality: "video", word: "clip", accept: "video/mp4", required: true },
    ]);
  });

  it("caps explicit slots at three, and a present empty list does not fall back to modalities", () => {
    const many = Array.from({ length: 4 }, (_, n) => ({ name: "frame_" + n, label: "picture " + n, modality: "image", accepts: ["image/*"] }));
    expect(composerInputs(declared(many)).media.map((input) => input.name)).toEqual(["frame_0", "frame_1", "frame_2"]);
    expect(composerInputs(declared([])).media).toEqual([]);
  });

  it("keeps the old single-input shape when the field is absent", () => {
    const old = declared();
    delete old.required_media_inputs;
    expect(composerInputs(old)).toEqual({
      text: "optional", media: [{ modality: "image", word: "picture", accept: "image/*", required: true }],
    });
  });
});
