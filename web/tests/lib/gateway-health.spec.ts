import { describe, it, expect } from "vitest";
import { gatewayHealthLabel } from "@/lib/gateway-health";
import type { CurrentHealth } from "@/lib/api";
const base = {
  admin: true,
  statusError: false,
  reachable: true,
  reachabilityError: false,
};
const health = (tone: string, observation_available = true): CurrentHealth => ({
  state: tone,
  tone,
  label: "",
  detail: "",
  observation_available,
  services: [],
});
describe("gateway health honesty", () => {
  it("distinguishes reachable degraded engines from an unreachable gateway", () => {
    expect(gatewayHealthLabel({ ...base, health: health("bad") }).label).toBe(
      "Degraded",
    );
    expect(
      gatewayHealthLabel({
        ...base,
        health: health("ok"),
        reachabilityError: true,
      }).label,
    ).toBe("Down");
  });
  it("never treats an unmeasured or stale projection as healthy", () => {
    expect(
      gatewayHealthLabel({ ...base, health: health("ok", false) }).label,
    ).toBe("Not measured yet");
    expect(
      gatewayHealthLabel({ ...base, health: health("ok"), statusError: true })
        .label,
    ).toBe("Status unavailable");
  });
  it("reports only reachability to members", () => {
    expect(
      gatewayHealthLabel({ ...base, admin: false, health: health("bad") })
        .label,
    ).toBe("Gateway reachable");
  });
});
