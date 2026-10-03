import { describe, expect, it } from "vitest";
import type { CurrentHealth } from "@/lib/api";
import {
  ownerEngineAlert,
  ownerProviderAlert,
} from "@/components/shell/use-owner-alert";
import { providerViews } from "@/lib/provider-status";

const health: CurrentHealth = {
  state: "warn",
  tone: "warn",
  label: "Needs attention",
  detail: "",
  observation_available: true,
  signals: [{ source: "hardware", tone: "bad", reasons: ["Disk is full"] }],
  services: [
    {
      service_id: "image",
      name: "Image engine",
      configured: true,
      enabled: true,
      role: "required",
      routable: false,
      observation: "down",
      tone: "bad",
      last_error: "Connection refused",
    },
  ],
};

describe("owner-wide alerts", () => {
  it("alerts only for explicitly enabled providers with an observed problem", () => {
    const rows = (enabled: unknown, reachable: unknown, key_set: unknown) =>
      providerViews({
        providers: [{ name: "Remote provider", enabled, reachable, key_set }],
      });
    expect(ownerProviderAlert(rows(true, false, true))).toContain(
      "is unreachable",
    );
    expect(ownerProviderAlert(rows(true, true, false))).toContain(
      "missing its provider key",
    );
    expect(ownerProviderAlert(rows(false, false, false))).toBeNull();
    expect(ownerProviderAlert(rows(undefined, false, false))).toBeNull();
    expect(ownerProviderAlert(rows(true, undefined, undefined))).toBeNull();
  });
  it("shows an explicit enabled engine outage", () => {
    expect(ownerEngineAlert(health)).toBe(
      "Image engine is unavailable. Check its connection on Status.",
    );
  });
  it("excludes disabled engines and host warnings", () => {
    expect(
      ownerEngineAlert({
        ...health,
        services: [{ ...health.services[0], enabled: false }],
      }),
    ).toBeNull();
    expect(ownerEngineAlert({ ...health, services: [] })).toBeNull();
  });
  it("does not call a missing or unavailable observation an outage", () => {
    expect(ownerEngineAlert()).toBeNull();
    expect(
      ownerEngineAlert({ ...health, observation_available: false }),
    ).toBeNull();
    expect(
      ownerEngineAlert({
        ...health,
        services: [{ ...health.services[0], observation: "unavailable" }],
      }),
    ).toBeNull();
  });
});
