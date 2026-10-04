import { describe, expect, it } from "vitest";
import {
  capabilitySummary,
  capabilityTiles,
  needsSetup,
  setupSteps,
} from "../presentation";
import type { HomeAlias, HomeCapability, SetupProgress } from "../home-api";

function progress(overrides: Partial<SetupProgress> = {}): SetupProgress {
  return {
    complete: false,
    steps: [
      {
        id: "connect_service",
        done: true,
        optional: false,
        reason: "A service is connected.",
        href: "/ui/engines/",
      },
      {
        id: "test_request",
        done: true,
        optional: false,
        reason: "A gateway request succeeded.",
        href: "#connect",
      },
      {
        id: "connect_app",
        done: true,
        optional: false,
        reason: "Legacy server step says an app key was used.",
        href: "#connect",
      },
    ],
    evidence: {
      gateway_request: { recorded: true, source: "receipt" },
      settings_copied: { recorded: false, source: "none" },
      external_client_confirmed: { recorded: false, source: "none" },
      receipt_viewed: { recorded: false, source: "none" },
    },
    ...overrides,
  };
}

describe("Home audit presentation contract", () => {
  it("uses observed external traffic to finish setup and respects dismissal", () => {
    const established = progress({ complete: true });
    established.evidence.observed_external_client = {
      recorded: true,
      source: "receipt",
    };
    expect(
      setupSteps(established).some((step) => step.id === "copy_settings"),
    ).toBe(false);
    expect(needsSetup(established)).toBe(false);
    expect(needsSetup(progress({ dismissed: true }))).toBe(false);
    expect(needsSetup(progress())).toBe(true);
  });

  it("keeps server music, sound effects and speech taxonomy distinct", () => {
    const rows = ["music", "sfx", "speech"].map(
      (kind): HomeCapability => ({
        capability: kind,
        title: kind,
        use_name: kind,
        intent: kind === "speech" ? "speech.synthesis" : "audio.generate",
        available: true,
        status: "ready",
        reason: "Ready.",
      }),
    );
    expect(capabilityTiles(rows, []).map((row) => row.capability)).toEqual([
      "music",
      "sfx",
      "speech",
    ]);
    expect(capabilitySummary(capabilityTiles(rows, []))).toContain(
      "sound effects",
    );
  });

  it("keeps gateway requests separate from external client setup evidence", () => {
    const steps = setupSteps(progress());
    expect(steps.find((step) => step.id === "connect_app")?.done).toBe(false);
    expect(steps.find((step) => step.id === "copy_settings")?.done).toBe(false);
    expect(steps.find((step) => step.id === "view_receipt")?.done).toBe(false);
  });

  it("reflects explicit setup evidence without inferring from receipt existence", () => {
    const steps = setupSteps(
      progress({
        evidence: {
          gateway_request: { recorded: true, source: "receipt" },
          settings_copied: { recorded: true, source: "user_confirmation" },
          external_client_confirmed: {
            recorded: true,
            source: "user_confirmation",
          },
          receipt_viewed: { recorded: true, source: "user_confirmation" },
        },
      }),
    );
    expect(steps.find((step) => step.id === "copy_settings")?.done).toBe(true);
    expect(steps.find((step) => step.id === "connect_app")?.done).toBe(true);
    expect(steps.find((step) => step.id === "view_receipt")?.done).toBe(true);
  });

  it("summarizes idle and unknown availability honestly", () => {
    const tiles: HomeCapability[] = [
      {
        capability: "chat",
        intent: "chat.completions",
        model: "fast",
        available: false,
        status: "idle",
        reason: "The model can be started.",
      },
      {
        capability: "image",
        intent: "image.generate",
        model: "image",
        available: false,
        status: "unknown",
        reason: "Not measured yet.",
      },
    ];
    expect(capabilitySummary(tiles)).toBe(
      "Chat is ready to start · images are not measured yet",
    );
  });

  it("projects scoped chat aliases into Home tiles", () => {
    const aliases: HomeAlias[] = [
      {
        id: "fast",
        operation: "chat.completions",
        available: true,
        status: "ready",
        reason: "Ready.",
      },
    ];
    const tiles = capabilityTiles([], aliases);
    expect(tiles).toMatchObject([
      { capability: "chat", model: "fast", status: "ready" },
    ]);
  });
});
