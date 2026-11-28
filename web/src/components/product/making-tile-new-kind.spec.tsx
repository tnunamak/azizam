import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MakingTile } from "./making-tile";

describe("making tiles for a new capability kind", () => {
  it("renders diagram with the File glyph and a visible generic phrase", () => {
    const html = renderToStaticMarkup(createElement(MakingTile, { kind: "diagram" }));
    expect(html).toContain("lucide-file");
    expect(html).toContain("Making your item");
    expect(html).not.toContain("undefined");
  });

  it("keeps the established image, video and audio glyphs and copy", () => {
    for (const [kind, glyph, copy] of [
      ["image", "lucide-image", "Making your picture"],
      ["video", "lucide-video", "Making your video"],
      ["audio", "lucide-audio-lines", "Making your audio"],
    ]) {
      const html = renderToStaticMarkup(createElement(MakingTile, { kind }));
      expect(html).toContain(glyph);
      expect(html).toContain(copy);
    }
  });

  it("keeps an unknown kind's warning and extra classes visible", () => {
    const html = renderToStaticMarkup(createElement(MakingTile, {
      kind: "diagram", risk: "Diagrams down: it may not finish", className: "custom-tile",
    }));
    expect(html).toContain("custom-tile");
    expect(html).toContain("Making your item");
    expect(html).toContain("Diagrams down: it may not finish");
  });

  it("does not treat inherited object keys as supported drawing kinds", () => {
    for (const kind of ["constructor", "toString", "__proto__"]) {
      const html = renderToStaticMarkup(createElement(MakingTile, { kind }));
      expect(html).toContain("lucide-file");
      expect(html).toContain("Making your item");
    }
  });
});
