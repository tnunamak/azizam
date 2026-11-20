import { describe, expect, it } from "vitest";
import { availabilityReason, stateReason, statusSummaryCopy } from "./status-copy";

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
  it("ends the no-capabilities message with a full stop and names what to do", () => {
    const copy = statusSummaryCopy({ state: "unavailable", reason_code: "no_capabilities" });
    expect(copy).toMatch(/\.$/);
    expect(copy).toMatch(/Ask the owner/i);
  });

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

// status-copy exports wording, not a style or tone. This table checks the
// remedy a caller can show without inferring the rendered component style.
const remedyCases = [
  ["ready", "ready", "Ready to use."],
  ["ready_on_request", "ready", "Ready to start"],
  ["idle", "ready", "Ready to start"],
  ["starting", "wait", "Try again shortly."],
  ["busy", "wait", "Try again shortly."],
  ["degraded", "wait", "Try again, and ask the owner if it continues."],
  ["engine_transitioning", "wait", "Try again shortly."],
  ["unknown", "error", "check the service"],
  ["unavailable", "error", "check this capability"],
  ["not_available", "error", "check this capability"],
  ["backend_unavailable", "error", "service is unavailable"],
  ["service_maintenance", "error", "down for maintenance"],
  ["health_unknown", "error", "status could not be checked"],
  ["unavailable_disabled", "setup", "enable this capability"],
  ["unavailable_unconfigured", "setup", "finish setup"],
  ["configured", "setup", "Set up but not started"],
  ["policy_denied", "setup", "changing access"],
  ["model_not_deployed", "setup", "not installed"],
  ["current_model_unavailable", "setup", "load one"],
  ["service_disabled", "setup", "turn the service on"],
  ["required_file_missing", "setup", "install it"],
] as const;

describe("availability copy remedies", () => {
  it("classifies every explicitly worded availability code", () => {
    expect(remedyCases.map(([code]) => code).sort()).toEqual(Object.keys(stateReason).sort());
  });

  it.each(remedyCases)("uses %s copy for a %s remedy", (code, remedy, expected) => {
    const copy = availabilityReason("unavailable", code);
    expect(copy).toContain(expected);
    if (remedy === "wait") {
      expect(copy).toMatch(/try again/i);
    } else {
      expect(copy).not.toMatch(/try again/i);
    }
    if (remedy === "setup" || remedy === "error") expect(copy).toContain("Ask the owner");
  });

  it("treats absent capabilities as account setup, not a transient wait", () => {
    const copy = statusSummaryCopy({ state: "unavailable", reason_code: "no_capabilities" });
    expect(copy).toBe("No capabilities are available to your account yet. Ask the owner to check your account access.");
    expect(copy).not.toMatch(/try again/i);
  });
});
