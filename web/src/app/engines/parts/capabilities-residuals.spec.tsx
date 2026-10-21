import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { CapabilityList, type CapabilityRow, type TestState } from "./capabilities";

afterEach(() => cleanup());

const rows: CapabilityRow[] = [
  { id: "chat", label: "Chat", level: "likely" },
  { id: "embeddings", label: "Embeddings", level: "likely" },
];

function passed(via: "saved" | "draft"): TestState {
  return {
    status: "done",
    result: {
      ok: true,
      at: 1_700_000_000,
      via,
      sent: "{}",
      sent_to: "http://localhost",
      reply: "ok",
      seconds: 0.1,
      cost: "unknown",
    } as never,
  };
}

describe("per-row test evidence", () => {
  it("uses each row's own saved or draft result", () => {
    render(
      <CapabilityList
        rows={rows}
        ticked={new Set(["chat", "embeddings"])}
        onTick={vi.fn()}
        tests={{ chat: passed("saved"), embeddings: passed("draft") }}
        testNote="Tested your unsaved changes from the gateway’s computer. The test did not save your changes and is not listed in Activity."
        storedNote="Tested the saved settings from the gateway’s computer. This test is not listed in Activity."
      />,
    );

    expect(within(screen.getByLabelText("Real test of Chat")).getByText(/Tested the saved settings/)).toBeVisible();
    expect(within(screen.getByLabelText("Real test of Embeddings")).getByText(/Tested your unsaved changes/)).toBeVisible();
  });
});
