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
