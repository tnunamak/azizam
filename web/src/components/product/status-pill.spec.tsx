import type { ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StatusPill } from "./status-pill";
import { StateBadge, StateIcon } from "./status-ui";

describe("StatusPill", () => {
  it.each(["constructor", "toString", "maintenance"])(
    "renders Unknown for %s without throwing",
    (status) => {
      const html = renderToStaticMarkup(<StatusPill status={status} />);
      expect(html).toContain("Unknown");
      expect(html).toContain("<svg");
      expect(html).toBe(renderToStaticMarkup(<StatusPill status="unknown" />));
    },
  );

  it("keeps ready's label, icon and colour", () => {
    const html = renderToStaticMarkup(<StatusPill status="ready" />);
    expect(html).toContain("Ready");
    expect(html).toContain("lucide-check");
    expect(html).toContain("text-success");
    expect(html).not.toContain("Unknown");
  });
});

describe("StateBadge", () => {
  it.each(["maintenance", "constructor", "toString"])(
    "renders the Unknown word for state %s",
    (state) => {
      const html = renderToStaticMarkup(
        <StateBadge state={state as ComponentProps<typeof StateBadge>["state"]} />,
      );
      expect(html).toContain("Unknown");
      expect(html).toContain("lucide-circle-dashed");
    },
  );
});

describe("StateIcon", () => {
  it.each(["maintenance", "constructor", "toString"])(
    "renders the unknown icon for tone %s",
    (tone) => {
      const html = renderToStaticMarkup(
        <StateIcon
          tone={tone as ComponentProps<typeof StateIcon>["tone"]}
          label="Status"
          className="custom-icon"
        />,
      );
      expect(html).toBe(renderToStaticMarkup(
        <StateIcon tone="unknown" label="Status" className="custom-icon" />,
      ));
      expect(html).toContain("<svg");
      expect(html).toContain('role="img"');
      expect(html).toContain('aria-label="Status"');
      expect(html).toContain("custom-icon");
    },
  );

  it("keeps a known tone's icon and colour", () => {
    const html = renderToStaticMarkup(<StateIcon tone="ok" />);
    expect(html).toContain("lucide-circle-check");
    expect(html).toContain("text-success");
    expect(html).toContain('aria-hidden="true"');
  });
});
