import { describe, expect, it } from "vitest";
import type { HomeCapability, HomeChat, HomeCreation, HomeReceipt } from "./home-api";
import {
  chooserRows,
  firstNameOf,
  formatWait,
  gatewayNeeds,
  greetingFor,
  kindOf,
  listFacts,
  makingRisk,
  makingTimeLeft,
  orderChooser,
  promptPlaceholder,
  recentItems,
  runFacts,
  workingSentence,
} from "./home-model";

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
    expect(listFacts(row({ icon_key: "chat", typical_seconds: 1 }))).toBe("Starts in about 1 s · no charge");
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

describe("H9 the owner's panel names operational facts only", () => {
  it("gives each need one action, and only the needs that exist", () => {
    const needs = gatewayNeeds({ engineFault: "Video studio stopped answering.", providerFault: null, noEngine: false });
    expect(needs).toEqual([
      {
        key: "engine",
        fact: "Video studio stopped answering.",
        action: { label: "Open Engines", href: "/engines/" },
      },
    ]);
    expect(gatewayNeeds({ engineFault: null, providerFault: null, noEngine: false })).toEqual([]);
  });

  it("sends a gateway with no engine to Engines", () => {
    const [need] = gatewayNeeds({ engineFault: null, providerFault: null, noEngine: true });
    expect(need.action.href).toBe("/engines/");
  });

  it("says what works in one sentence", () => {
    const view = (key: string, state: string) => ({ key, label: key, state }) as never;
    expect(workingSentence([view("chat", "ready"), view("image", "ready"), view("speech", "ready")])).toBe(
      "Chat, Images and Voice are working.",
    );
    expect(workingSentence([view("video", "off")])).toBeNull();
  });
});

describe("greeting and names", () => {
  it("greets by time of day and welcomes a first visit", () => {
    expect(greetingFor(new Date(2026, 9, 23, 19), "Robin", false)).toBe("Good evening, Robin");
    expect(greetingFor(new Date(2026, 9, 23, 9), null, false)).toBe("Good morning");
    expect(greetingFor(new Date(2026, 9, 23, 13), "June", true)).toBe("Welcome, June");
  });

  it("uses the first word of a name", () => {
    expect(firstNameOf("Robin Okafor")).toBe("Robin");
    expect(firstNameOf("  ")).toBeNull();
    expect(firstNameOf("robin@example.test")).toBeNull();
  });
});

describe("H13 no model or engine names in the Home code", () => {
  it("gives placeholders by kind, with a plain default", () => {
    expect(promptPlaceholder("image")).toBe("Describe the picture you want");
    expect(promptPlaceholder("made-up-kind")).toBe("Describe what you want");
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
