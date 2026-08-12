import { describe, expect, it } from "vitest";

import type { CurrentHealth, CurrentHealthService } from "../../src/lib/api";
import { deriveServicesHealthSummary } from "../../src/lib/services-health-summary";
import producerHealth from "../fixtures/gateway/current-health-routing-unknown.json";

function service(
  overrides: Partial<CurrentHealthService>,
): CurrentHealthService {
  return {
    service_id: "svc",
    name: "Service",
    configured: true,
    enabled: true,
    role: "required",
    routable: true,
    observation: "up",
    tone: "ok",
    last_error: null,
    ...overrides,
  };
}

function health(
  services: CurrentHealthService[],
  incomplete?: CurrentHealth["incomplete"],
): CurrentHealth {
  return {
    state: "ok",
    tone: "ok",
    label: "Operational",
    detail: "Current configured-service health is normal.",
    observation_available: true,
    services,
    ...(incomplete ? { incomplete } : {}),
  };
}

describe("Services health summary", () => {
  it("keeps required-down bad", () => {
    expect(
      deriveServicesHealthSummary(
        health([service({ observation: "down", tone: "bad" })]),
        1,
        1,
      ),
    ).toMatchObject({
      metricTone: "bad",
      badCount: 1,
      incidentTone: "bad",
    });
  });

  it("keeps optional-down and degraded warning without calling either unreachable", () => {
    const summary = deriveServicesHealthSummary(
      health([
        service({ observation: "down", tone: "warn", role: "optional" }),
        service({ service_id: "degraded", observation: "degraded", tone: "warn" }),
      ]),
      2,
      2,
    );

    expect(summary).toMatchObject({
      metricTone: "warn",
      warnCount: 2,
      incidentTone: "warn",
    });
    expect(summary.metricLabel).not.toContain("unreachable");
  });

  it("keeps disabled and stopped standby neutral", () => {
    expect(
      deriveServicesHealthSummary(
        health([
          service({ observation: "disabled", tone: "neutral", enabled: false }),
          service({
            service_id: "standby",
            observation: "standby_stopped",
            tone: "neutral",
            role: "standby",
          }),
        ]),
        0,
        2,
      ),
    ).toMatchObject({ metricTone: "neutral", incidentTone: null });
  });

  it("keeps routing unknown explicit and neutral when no known severity exists", () => {
    const summary = deriveServicesHealthSummary(
      producerHealth.routingUnknown,
      1,
      1,
    );

    expect(summary).toMatchObject({
      metricTone: "neutral",
      incompleteCount: 1,
      incidentTone: null,
    });
    expect(summary.metricLabel).toBe("enabled · 1 incomplete");
  });

  it("keeps a known bad above simultaneous incomplete coverage", () => {
    const summary = deriveServicesHealthSummary(
      producerHealth.badWithRoutingUnknown,
      2,
      2,
    );

    expect(summary).toMatchObject({
      metricTone: "bad",
      badCount: 1,
      incompleteCount: 1,
      incidentTone: "bad",
    });
    expect(summary.metricLabel).toBe("enabled · 1 action needed");
  });
});
