import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { EmptyOperationalState, IncidentBanner } from "@/components/ui";
import { StatusIcon } from "@/components/ui/icons";

afterEach(cleanup);

describe("shared operational primitives", () => {
  it.each([
    ["ok", "text-success"],
    ["warn", "text-warning"],
    ["bad", "text-danger"],
    ["info", "text-info"],
    ["neutral", "text-muted-foreground"],
  ] as const)("renders the explicit %s StatusIcon colour class", (tone, colourClass) => {
    render(<StatusIcon tone={tone} label="Engine state" className="fixture" />);
    expect(screen.getByRole("img", { name: "Engine state" })).toHaveClass(
      "vf-status-icon",
      `vf-status-icon-${tone}`,
      "size-[1em]",
      "flex-none",
      colourClass,
      "fixture",
    );
  });

  it.each(["warn", "bad", "info"] as const)(
    "preserves the %s incident role, content and tone",
    (tone) => {
      render(
        <IncidentBanner
          tone={tone}
          title="Engine unavailable"
          detail="Try again later."
          className="fixture"
        />,
      );
      const banner = screen.getByRole(tone === "bad" ? "alert" : "status");
      expect(banner).toHaveClass(
        "vf-incident-banner",
        `vf-incident-banner-${tone}`,
        "bg-transparent",
        "fixture",
      );
      expect(screen.getByText("Engine unavailable")).toHaveClass(
        "vf-incident-banner-title",
      );
      expect(screen.getByText("Try again later.")).toHaveClass(
        "vf-incident-banner-body",
      );
      expect(banner.querySelector(".vf-incident-banner-actions")).toBeNull();
    },
  );

  it("keeps incident actions and dismissal independently operable", async () => {
    const user = userEvent.setup();
    const action = vi.fn();
    const dismiss = vi.fn();
    const { rerender } = render(
      <IncidentBanner
        tone="bad"
        title="Engine unavailable"
        actions={<button onClick={action}>Retry</button>}
        onDismiss={dismiss}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(action).toHaveBeenCalledOnce();
    expect(dismiss).not.toHaveBeenCalled();
    const button = screen.getByRole("button", { name: "Dismiss" });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("vf-button", "vf-button-ghost", "vf-button-sm");
    await user.click(button);
    expect(dismiss).toHaveBeenCalledOnce();

    rerender(<IncidentBanner tone="info" title="Waiting" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.querySelector(".vf-incident-banner-body")).toBeNull();
  });

  it.each(["neutral", "info", "warn", "bad"] as const)(
    "preserves the %s empty state content and tone",
    (tone) => {
      render(
        <EmptyOperationalState
          tone={tone}
          title="No engines"
          detail="Add an engine to start."
          action={<button>Add engine</button>}
          className="fixture"
        />,
      );
      const title = screen.getByText("No engines");
      expect(title).toHaveClass("vf-empty-operational-title");
      expect(title.parentElement).toHaveClass("vf-empty-operational", "fixture");
      if (tone !== "neutral") expect(title).toHaveClass(`text-vf-${tone}`);
      else {
        expect(title).toHaveClass("vf-empty-operational-title", "text-foreground");
        for (const toneClass of ["text-vf-warn", "text-vf-bad", "text-vf-info"]) {
          expect(title).not.toHaveClass(toneClass);
        }
      }
      expect(screen.getByText("Add an engine to start.")).toHaveClass(
        "vf-empty-operational-body",
      );
      expect(screen.getByRole("button", { name: "Add engine" }).parentElement)
        .toHaveClass("vf-empty-operational-actions");
    },
  );

  it("keeps a neutral empty state minimal when optional content is absent", () => {
    const { container } = render(<EmptyOperationalState title="No engines" />);
    expect(screen.getByText("No engines")).toHaveClass(
      "vf-empty-operational-title", "text-foreground",
    );
    expect(container.querySelector(".vf-empty-operational-body")).toBeNull();
    expect(container.querySelector(".vf-empty-operational-actions")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
