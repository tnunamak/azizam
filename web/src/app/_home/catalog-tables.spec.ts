import { expect, it } from "vitest";
import { recentItems, tryHasOwnTab } from "./home-model";
import type { HomeCapability, HomeReceipt } from "./home-api";

const MEDIA_KINDS = ["image", "video", "audio"];

function running(intent: string): HomeReceipt {
  return { request_id: "r", intent, status: "running" } as HomeReceipt;
}

function making(row: HomeCapability, mediaOutputKinds: readonly string[]) {
  return recentItems({ rows: [row], receipts: [running(row.intent)], media: [], chats: [], mediaOutputKinds })[0];
}

it("recognizes a tab added in the catalog", () => {
  expect(tryHasOwnTab("diagram", ["diagram"])).toBe(true);
  expect(tryHasOwnTab("chat", ["diagram"])).toBe(false);
});

it("reads today's seven tab kinds from the catalog's own try words", () => {
  for (const kind of ["chat", "image", "speech", "transcribe", "decide", "embed", "mesh"]) {
    expect(tryHasOwnTab(kind)).toBe(true);
  }
  for (const kind of ["video", "music", "sfx", "workflow", "diagram"]) {
    expect(tryHasOwnTab(kind)).toBe(false);
  }
});

it("names a media modality added in the catalog", () => {
  const row = { capability: "diagram", intent: "diagram.generate", output_modalities: ["blueprint"] } as HomeCapability;
  expect(making(row, ["blueprint"])).toMatchObject({ making: "blueprint" });
});

it("keeps today's names for image, video and audio, and calls any other output an item", () => {
  const row = (modality: string) =>
    ({ capability: "x", intent: "x.make", output_modalities: ["text", modality] }) as HomeCapability;
  expect(making(row("image"), MEDIA_KINDS)).toMatchObject({ making: "image" });
  expect(making(row("video"), MEDIA_KINDS)).toMatchObject({ making: "video" });
  expect(making(row("audio"), MEDIA_KINDS)).toMatchObject({ making: "audio" });
  expect(making(row("mesh"), MEDIA_KINDS)).toMatchObject({ making: "item" });
});

it("calls an output item when the gateway names no media kinds", () => {
  const row = { capability: "x", intent: "x.make", output_modalities: ["image"] } as HomeCapability;
  expect(making(row, [])).toMatchObject({ making: "item" });
});

it("holds the intent fallback to the gateway's making kinds, as declared outputs are", () => {
  const legacy = { capability: "x", intent: "image.generate" } as HomeCapability;
  expect(making(legacy, MEDIA_KINDS)).toMatchObject({ making: "image" });
  expect(making(legacy, ["video", "audio"])).toMatchObject({ making: "item" });
});
