import { describe, expect, it } from "vitest";
import { AudioLines, Box, CircleHelp, File, ImageIcon, MessageSquare } from "lucide-react";
import { capabilityMeta } from "./readiness";

describe("a kind of work's mark", () => {
  it("is the named mark for a kind this UI names, whatever the row declares", () => {
    expect(capabilityMeta("chat", ["image"]).icon).toBe(MessageSquare);
  });

  it("comes from what an unnamed kind makes, by its IANA top-level type", () => {
    expect(capabilityMeta("edit", ["image"]).icon).toBe(ImageIcon);
    expect(capabilityMeta("narrate", ["Audio"]).icon).toBe(AudioLines);
    expect(capabilityMeta("sculpt", ["model"]).icon).toBe(Box);
  });

  it("is a plain file for a type with no icon, and the question mark only when nothing is declared", () => {
    expect(capabilityMeta("smell", ["smell"]).icon).toBe(File);
    expect(capabilityMeta("smell").icon).toBe(CircleHelp);
    expect(capabilityMeta("smell", []).icon).toBe(CircleHelp);
  });
});
