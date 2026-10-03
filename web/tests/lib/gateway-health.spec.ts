import { describe, it, expect } from "vitest";
import { gatewayHealthLabel, gatewayHealthReason } from "@/lib/gateway-health";
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
  it("explains an enabled measured service outage", () => {
    const current = health("bad");
    current.services = [
      {
        service_id: "images",
        name: "Image engine",
        configured: true,
        enabled: true,
        role: "required",
        routable: false,
        observation: "down",
        tone: "bad",
        last_error: "Internal diagnostic must stay out of member UI",
      },
    ];
    expect(gatewayHealthReason({ ...base, health: current })).toBe(
      "Image engine is unavailable. Check its connection on Status.",
    );
    current.services[0].enabled = false;
    expect(gatewayHealthReason({ ...base, health: current })).not.toContain(
      "Image engine",
    );
  });
  it("never exposes owner health details to a member", () => {
    const current = { ...health("bad"), detail: "Private operator diagnostic" };
    expect(
      gatewayHealthReason({ ...base, admin: false, health: current }),
    ).toBe("The gateway is reachable; model availability is shown on Home.");
    expect(
      gatewayHealthReason({
        ...base,
        admin: false,
        health: current,
        memberStatus: {
          state: "unavailable",
          reason: "Images are unavailable right now.",
        },
      }),
    ).toBe("Images are unavailable right now.");
  });
  it("explains unknown health and failed observations without claiming readiness", () => {
    expect(gatewayHealthReason({ ...base, statusError: true })).toContain(
      "could not be loaded",
    );
    expect(gatewayHealthReason({ ...base })).toContain("not been measured");
    expect(gatewayHealthReason({ ...base, reachabilityError: true })).toContain(
      "could not be reached",
    );
  });
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
