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
        reason_code: "ready",
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

  it("does not add manual client setup evidence steps", () => {
    const steps = setupSteps(progress());
    expect(steps.map((step) => step.id)).toEqual([
      "connect_service",
      "test_request",
      "connect_app",
    ]);
    expect(steps.find((step) => step.id === "copy_settings")).toBeUndefined();
    expect(steps.find((step) => step.id === "view_receipt")).toBeUndefined();
  });

  it("leaves setup evidence in the progress payload instead of rendering confirmation steps", () => {
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
    expect(steps.find((step) => step.id === "copy_settings")).toBeUndefined();
    expect(steps.find((step) => step.id === "view_receipt")).toBeUndefined();
    expect(steps.find((step) => step.id === "connect_app")?.done).toBe(true);
  });

  it("summarizes idle and unknown availability honestly", () => {
    const tiles: HomeCapability[] = [
      {
        capability: "chat",
        intent: "chat.completions",
        model: "fast",
        available: false,
        status: "idle",
        reason_code: "ready_on_request",
      },
      {
        capability: "image",
        intent: "image.generate",
        model: "image",
        available: false,
        status: "unknown",
        reason_code: "unknown",
      },
    ];
    expect(capabilitySummary(tiles)).toBe("Chat is ready to start");
  });

  it("projects scoped chat aliases into Home tiles", () => {
    const aliases: HomeAlias[] = [
      {
        id: "utility-check",
        operation: "chat.completions",
        available: true,
        status: "ready",
        reason_code: "ready",
      },
      {
        id: "default",
        operation: "chat.completions",
        available: true,
        status: "ready",
        reason_code: "ready",
      },
    ];
    const tiles = capabilityTiles([], aliases);
    expect(tiles).toMatchObject([
      { capability: "chat", model: "default", status: "ready" },
    ]);
    expect(capabilityTiles([], [aliases[0]])).toEqual([]);
  });

  it("uses representative rows instead of first available titled rows", () => {
    const tiles = capabilityTiles(
      [
        {
          capability: "chat",
          title: "Utility check",
          use_name: "utility-check",
          intent: "chat.completions",
          model: "utility-check",
          available: true,
          status: "ready",
          reason_code: "ready",
        },
        {
          capability: "chat",
          title: "Recommended chat",
          use_name: "default",
          intent: "chat.completions",
          model: "default",
          available: false,
          status: "idle",
          reason_code: "ready_on_request",
        },
      ],
      [],
    );
    expect(tiles).toMatchObject([{ capability: "chat", model: "default" }]);
  });
});
