import { describe, expect, it } from "vitest";
import { ownerEngineAlert } from "./use-owner-alert";
import type { CurrentHealth } from "@/lib/api";

function health(inUse: boolean | null, enabled = true): CurrentHealth {
  return {
    state: "bad",
    tone: "bad",
    label: "Action needed",
    detail: "Engine down",
    observation_available: true,
    services: [
      {
        service_id: "engine",
        name: "Engine",
        configured: true,
        enabled,
        role: "required",
        routable: null,
        in_use: inUse,
        observation: "down",
        tone: "bad",
        last_error: null,
      },
    ],
  };
}

describe("owner engine alerts", () => {
  it("requires enabled and proven in-use engines", () => {
    expect(ownerEngineAlert(health(false))).toBeNull();
    expect(ownerEngineAlert(health(null))).toBeNull();
    expect(ownerEngineAlert(health(true, false))).toBeNull();
    expect(ownerEngineAlert(health(true))).toMatch(/Engine is unavailable/);
  });
});

it("uses the observed display label, including long labels", () => {
  const observed = health(true);
  const label = "Azizam owner engine ".repeat(20);
  observed.services[0].name = "internal-service";
  observed.services[0].engine_label = label;
  expect(ownerEngineAlert(observed)).toBe(
    `${label} is unavailable. Check its connection on Status.`,
  );
});

it("prefers the registry display name over an observation label", () => {
  const observed = health(true);
  observed.services[0].engine_label = "Observation name";
  expect(
    ownerEngineAlert(observed, {
      schema_version: 1,
      services: {
        engine: {
          id: "engine",
          name: "internal",
          display_name: "Azizam chat",
          kind: "generic",
          source: "ui",
          editable: true,
          enabled: true,
          capabilities: [],
          config: {},
        },
      },
    }),
  ).toBe("Azizam chat is unavailable. Check its connection on Status.");
});
