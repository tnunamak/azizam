import { expect, it } from "vitest";
import { capabilityLabels } from "./terms";
import { capabilityMeta } from "@/components/product/readiness";
import { humanCapabilities, humanCapability } from "@/app/engines/engine-groups";
import { statusSummaryCopy } from "./status-copy";
import { capabilityLabel } from "./use-capability-views";

it.each(Object.entries(capabilityLabels))(
  "%s uses its shared capability label on web surfaces",
  (id, label) => {
    expect(capabilityLabel(id)).toBe(label);
    expect(humanCapability(id)).toBe(label);
    expect(humanCapabilities([id])).toEqual([label]);
    expect(capabilityMeta(id).label).toBe(label);
    expect(
      statusSummaryCopy({
        state: "unavailable",
        reason_code: "unavailable_disabled",
        affected_capability_ids: [id],
      }),
    ).toContain(label);
  },
);

it("resolves known protocol spellings through the shared capability label", () => {
  expect(humanCapabilities(["speech", "tts"])).toEqual([capabilityLabels.speech]);
  expect(capabilityLabel("voice")).toBe(capabilityLabels.speech);
});
