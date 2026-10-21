import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { EngineRecordV2 } from "@/lib/engines-api";
import { engineView } from "../__fixtures__/test-utils";
import { ObservationList, OfferingFacts } from "./contract-sections";

afterEach(() => cleanup());

const baseRecord = {
  schema_version: 2,
  id: "engine",
  name: "engine",
  enabled: true,
  record_type: "engine",
  derived: false,
  editable: true,
  provenance: { source: "ui", package: null },
  connection: { transport: "http", address: "http://localhost", protocol: "openai-compatible", credentials: { inference: null, control: null }, headers: [], timeouts: {} },
  offerings: [],
  process: { driver: "none" },
  residency: {},
  resources: {},
} as unknown as EngineRecordV2;

function record(overrides: Partial<EngineRecordV2>): EngineRecordV2 {
  return { ...baseRecord, observations: {}, ...overrides };
}

describe("historical contract observations", () => {
  it("keeps a positive observation without a time visibly historical and unknown-age", () => {
    const view = engineView({ v2: record({ observations: { inventory: { http: { path: "/models" }, last: { ok: true } } } as never }) });
    render(<ObservationList view={view} />);

    const row = screen.getByText("Which models it has").closest("li")!;
    expect(row).toHaveTextContent("Historical result");
    expect(row).toHaveTextContent("Check time unknown");
    expect(row.querySelector('[aria-label="Verified by a check"]')).toBeNull();
  });

  it("does not turn an observation without a result boolean into a failure", () => {
    const view = engineView({ v2: record({ observations: { inventory: { http: { path: "/models" }, last: { detail: "partial response" } } } as never }) });
    render(<ObservationList view={view} />);

    const row = screen.getByText("Which models it has").closest("li")!;
    expect(row).toHaveTextContent("Result incomplete; success or failure is unknown");
    expect(row).not.toHaveTextContent("not as expected");
  });

  it("labels the saved selection as the last observed selection", () => {
    render(<OfferingFacts v2={record({
      offerings: [{ id: "model-a", kind: "model", selection: { mode: "switch" } }],
      selected_offering: "model-a",
      observations: { selected: { last: { ok: true, at: 1_700_000_000, source: "server" } } },
    })} />);

    expect(screen.getByTestId("offering-facts")).toHaveTextContent("Last observed selection:");
    expect(screen.getByTestId("offering-facts")).not.toHaveTextContent("Selected now:");
  });
});
