import { describe, expect, it } from "vitest";
import { availabilityReason, statusSummaryCopy } from "@/lib/status-copy";

describe("status copy", () => {
  it("phrases current states and omits unknown observations", () => {
    expect(availabilityReason("idle", "ready_on_request")).toBe(
      "Ready to start when you make a request.",
    );
    expect(availabilityReason("unknown", "unknown")).toBeUndefined();
    expect(availabilityReason("degraded", "degraded")).toBe(
      "Degraded. Requests may fail.",
    );
  });

  it("uses the capability label and matching plural verb once", () => {
    expect(
      statusSummaryCopy({
        state: "unavailable",
        reason_code: "unavailable",
        affected_capability_ids: ["image", "video"],
        counts: { affected: 2 },
      }),
    ).toBe("Images and video are unavailable.");
  });

  it("does not narrate a healthy summary", () => {
    expect(
      statusSummaryCopy({
        state: "ready",
        reason_code: "ready",
        affected_capability_ids: [],
        counts: { affected: 0 },
      }),
    ).toBeUndefined();
  });
});
