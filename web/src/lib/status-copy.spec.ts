import { describe, expect, it } from "vitest";
import { availabilityReason, stateReason } from "./status-copy";

// Closed gateway vocabulary: gateway/availability_reasons.py, REASONS.
const gatewayReasons = [
  "backend_unavailable",
  "engine_transitioning",
  "model_not_deployed",
  "current_model_unavailable",
  "service_disabled",
  "service_maintenance",
  "health_unknown",
  "required_file_missing",
] as const;

describe("availability reason copy", () => {
  it.each(gatewayReasons)("explains %s without naming internals", (code) => {
    const copy = availabilityReason("unavailable", code);
    expect(copy).toMatch(/\S.*\.$/);
    expect(copy).not.toMatch(/\b(?:engine|comfyui|backend)\b|[\w-]+\.(?:py|json|ya?ml|safetensors|ckpt|tsx?)\b/i);
    if (code === "engine_transitioning") {
      expect(copy).toContain("Try again shortly.");
    } else {
      expect(copy).toContain("Ask the owner");
      expect(copy).not.toContain("Try again shortly.");
    }
  });

  it("asks the owner to check the service when health is unknown", () => {
    const copy = availabilityReason("unavailable", "health_unknown");
    expect(copy).toContain("Ask the owner to check its service");
    expect(copy).not.toMatch(/try again/i);
  });

  it("suggests retrying only while a gateway availability reason is transitioning", () => {
    const retryReasons = gatewayReasons.filter((code) =>
      /try again/i.test(availabilityReason("unavailable", code) ?? "")
    );
    expect(retryReasons).toEqual(["engine_transitioning"]);
  });

  it.each([
    ["starting", "Starting. Try again shortly."],
    ["unavailable_disabled", "Unavailable. Ask the owner to enable this capability."],
    ["policy_denied", "Unavailable under this access plan. Ask the owner about changing access."],
  ])("preserves existing wording for %s", (code, expected) => {
    expect(availabilityReason("unavailable", code)).toBe(expected);
  });

  it("gives every unavailable state or reason punctuated copy with a next step", () => {
    for (const code of Object.keys(stateReason)) {
      const copy = availabilityReason(code);
      expect(copy, code).toMatch(/\S.*\.$/);
      if (code === "ready") {
        // An already usable capability does not need remediation.
        expect(copy, code).toBe("Ready to use.");
      } else {
        expect(copy, code).toMatch(/Ask the owner|Try again|Ready to start/);
      }
    }
  });

  it("shares wording only between explicit same-remedy pairs", () => {
    const entries = Object.entries(stateReason).sort(([a], [b]) => a.localeCompare(b));
    const sameText = entries.flatMap(([a, copy], index) =>
      entries.slice(index + 1).filter(([, other]) => other === copy).map(([b]) => [a, b])
    );
    expect(sameText).toEqual([
      ["idle", "ready_on_request"],
      ["not_available", "unavailable"],
    ]);
  });
});
