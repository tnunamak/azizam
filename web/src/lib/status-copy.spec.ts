import { describe, expect, it } from "vitest";
import { availabilityReason } from "./status-copy";

// Closed gateway vocabulary: gateway/availability_reasons.py, REASONS.
const gatewayReasons = [
  "backend_unavailable",
  "engine_transitioning",
  "model_not_deployed",
  "current_model_unavailable",
  "service_disabled",
  "service_maintenance",
  "health_unknown",
] as const;

describe("availability reason copy", () => {
  it.each(gatewayReasons)("explains %s without naming internals", (code) => {
    const copy = availabilityReason("unavailable", code);
    expect(copy).toMatch(/\S.*\.$/);
    expect(copy).not.toMatch(/\b(?:engine|comfyui|backend)\b|[\w-]+\.(?:py|json|ya?ml|safetensors|ckpt|tsx?)\b/i);
    if (code === "engine_transitioning" || code === "health_unknown") {
      expect(copy).toContain("Try again shortly.");
    } else {
      expect(copy).toContain("Ask the owner");
      expect(copy).not.toContain("Try again shortly.");
    }
  });

  it.each([
    ["starting", "Starting. Try again shortly."],
    ["unavailable_disabled", "Unavailable. Ask the owner to enable this capability."],
    ["policy_denied", "Unavailable under this access plan."],
  ])("preserves existing wording for %s", (code, expected) => {
    expect(availabilityReason("unavailable", code)).toBe(expected);
  });
});
