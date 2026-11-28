import { describe, expect, it } from "vitest";
import { ownerEngineAlert, ownerProviderAlert } from "./use-owner-alert";
import { providerViews } from "@/lib/provider-status";
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
    expect(ownerEngineAlert(health(true))).toMatch(/Engine can’t take requests/);
  });
});

it("uses the observed display label, including long labels", () => {
  const observed = health(true);
  const label = "Azizam owner engine ".repeat(20);
  observed.services[0].name = "internal-service";
  observed.services[0].engine_label = label;
  expect(ownerEngineAlert(observed)).toBe(
    `${label} can’t take requests right now.`,
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
  ).toBe("Azizam chat can’t take requests right now.");
});

describe("provider alerts for a subscription", () => {
  const subscription = (connectionStatus: string, reachable: boolean | null) =>
    providerViews([
      {
        id: "chatgpt", name: "ChatGPT plan", enabled: true, reachable,
        key_status: { state: "connection", source: "connection", connection_status: connectionStatus },
      },
    ]);

  it("says nothing about a connected subscription, which holds no key", () => {
    expect(ownerProviderAlert(subscription("connected", true))).toBeNull();
  });

  it("says nothing about a subscription that is not connected", () => {
    expect(ownerProviderAlert(subscription("not_connected", null))).toBeNull();
  });

  it("asks for a new sign-in, not a key, when the sign-in ended", () => {
    expect(ownerProviderAlert(subscription("needs_reconnect", false))).toBe("ChatGPT plan needs a new sign-in.");
  });

  it("still asks for a key when a stored-key provider has none", () => {
    const views = providerViews([{ id: "p", name: "Paid one", enabled: true, key_status: { state: "missing" } }]);
    expect(ownerProviderAlert(views)).toBe("Paid one needs a provider key.");
  });
});
