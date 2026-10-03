import { describe, expect, it } from "vitest";

import {
  detectedModelsPatch,
  normalizeModelConfigForPatch,
  type DetectedModelRow,
} from "../../src/app/engines/detected-models";

const selected = (
  upstream: string,
  publicName = upstream,
): DetectedModelRow => ({
  upstream,
  publicName,
  selected: true,
});

describe("detected model helpers", () => {
  it("adds selected detected models with schema version 1", () => {
    expect(detectedModelsPatch([], [selected("demo-chat")])).toEqual({
      model_policy_schema_version: 1,
      models: [
        {
          id: "demo-chat",
          model_group: "demo-chat",
          upstream_model: "demo-chat",
          transforms: [],
        },
      ],
    });
  });

  it("keeps existing mappings and unknown policy fields", () => {
    const patch = detectedModelsPatch(
      [
        {
          id: "public-chat",
          model_group: "chat",
          upstream_model: "llama",
          routing_hint: "keep",
          request_policy: { max_tokens: 2048 },
          unknown_future_policy: { keep: true },
        },
      ],
      [selected("qwen", "public-qwen")],
    );

    expect(patch).toEqual({
      model_policy_schema_version: 1,
      models: [
        {
          id: "public-chat",
          model_group: "chat",
          upstream_model: "llama",
          routing_hint: "keep",
          request_policy: { max_tokens: 2048 },
          unknown_future_policy: { keep: true },
        },
        {
          id: "public-qwen",
          model_group: "public-qwen",
          upstream_model: "qwen",
          transforms: [],
        },
      ],
    });
  });

  it("normalizes object-shaped legacy mappings before patching", () => {
    expect(
      normalizeModelConfigForPatch({
        "legacy-chat": {
          upstream_model: "legacy-upstream",
          routing_hint: "keep-me",
        },
        slot: {
          model_group: "public",
          deployment: "real",
          routing_hint: "keep-slot",
        },
      }),
    ).toEqual([
      {
        id: "legacy-chat",
        model_group: "legacy-chat",
        upstream_model: "legacy-upstream",
        routing_hint: "keep-me",
      },
      {
        id: "slot",
        model_group: "public",
        upstream_model: "real",
        deployment: "real",
        routing_hint: "keep-slot",
      },
    ]);
  });

  it("rejects empty, duplicate, wildcard, spaced, and colliding public names", () => {
    expect(() => detectedModelsPatch([], [selected("demo", "")])).toThrow(
      "Public names must be nonempty",
    );
    expect(() => detectedModelsPatch([], [selected("demo", "*")])).toThrow(
      "Public names must be nonempty",
    );
    expect(() =>
      detectedModelsPatch([], [selected("demo", "public chat")]),
    ).toThrow("Public names must be nonempty");
    expect(() =>
      detectedModelsPatch([], [selected("a", "chat"), selected("b", "CHAT")]),
    ).toThrow("used more than once");
    expect(() =>
      detectedModelsPatch(
        [{ id: "chat", model_group: "chat", upstream_model: "old" }],
        [selected("new", "chat")],
      ),
    ).toThrow("already configured");
  });

  it("does not duplicate an existing mapping for the same upstream", () => {
    expect(
      detectedModelsPatch(
        [{ id: "chat", model_group: "chat", upstream_model: "demo" }],
        [selected("demo", "chat")],
      ),
    ).toEqual({
      model_policy_schema_version: 1,
      models: [{ id: "chat", model_group: "chat", upstream_model: "demo" }],
    });
  });

  it("rejects unsupported existing model shapes and empty selections", () => {
    expect(() =>
      detectedModelsPatch("raw-native-config", [selected("demo")]),
    ).toThrow("Existing model settings need review");
    expect(() =>
      detectedModelsPatch([{ id: "chat" }], [
        { upstream: "demo", publicName: "demo", selected: false },
      ]),
    ).toThrow("Select at least one detected model");
  });
});
