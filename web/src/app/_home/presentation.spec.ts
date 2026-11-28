import { describe, expect, it } from "vitest";
import type { HomeCapability } from "./home-api";
import { capabilitySummary } from "./presentation";

/** A capability tile with only the fields the summary reads. */
function tile(capability: string, status: string): HomeCapability {
  return { capability, status } as unknown as HomeCapability;
}

describe("capabilitySummary", () => {
  it("reads a mesh-only summary in the plural", () => {
    expect(capabilitySummary([tile("mesh", "ready")])).toBe("3D models are ready");
  });

  it("keeps the singular for one ordinary capability", () => {
    expect(capabilitySummary([tile("chat", "ready")])).toBe("Chat is ready");
  });

  it("joins mesh with other ready capabilities", () => {
    expect(capabilitySummary([tile("chat", "ready"), tile("mesh", "ready")])).toBe("Chat and 3D models are ready");
  });
});
