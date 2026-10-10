import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Button as ModernButton } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { Button as LegacyButton } from "@/components/ui";
import { Switch as ModernSwitch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

afterEach(cleanup);

describe("phone target contracts", () => {
  it("preserves modern button clicks, links and disabled state", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <>
        <ModernButton size="xs" onClick={onClick}>
          Copy
        </ModernButton>
        <ModernButton asChild size="icon-xs">
          <a href="#alias">Alias</a>
        </ModernButton>
        <ModernButton disabled>Unavailable</ModernButton>
      </>,
    );

    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("link", { name: "Alias" })).toHaveAttribute(
      "href",
      "#alias",
    );
    expect(screen.getByRole("button", { name: "Unavailable" })).toBeDisabled();
  });

  it("preserves legacy button keyboard activation and links", async () => {
    const user = userEvent.setup();
    const onLegacyClick = vi.fn();
    const onIconClick = vi.fn();
    render(
      <>
        <LegacyButton size="sm" onClick={onLegacyClick}>
          Test
        </LegacyButton>
        <LegacyButton asChild size="sm">
          <a href="#edit">Edit</a>
        </LegacyButton>
        <IconButton size="sm" aria-label="Close" onClick={onIconClick}>
          x
        </IconButton>
      </>,
    );

    await user.keyboard("{Tab}{Enter}");
    await user.keyboard("{Tab}{Tab}{Enter}");
    expect(onLegacyClick).toHaveBeenCalledOnce();
    expect(onIconClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute(
      "href",
      "#edit",
    );
  });

  it("keeps modern switch state and keyboard handling on one root", async () => {
    const user = userEvent.setup();
    render(<ModernSwitch aria-label="Enable engine" size="sm" />);

    const control = screen.getByRole("switch", { name: "Enable engine" });
    await user.click(control);
    expect(control).toBeChecked();
    expect(control).toHaveFocus();
    await user.keyboard(" ");
    expect(control).not.toBeChecked();
    expect(control.querySelector('[data-slot="switch-track"]')).not.toBeNull();
    expect(screen.getAllByRole("switch")).toHaveLength(1);
  });

  it("preserves tab keyboard navigation and disabled state", async () => {
    const user = userEvent.setup();
    render(
      <Tabs defaultValue="details">
        <TabsList aria-label="Example detail">
          <TabsTrigger value="details">Details</TabsTrigger>
          <TabsTrigger value="empty">Empty</TabsTrigger>
          <TabsTrigger value="disabled" disabled>
            Disabled
          </TabsTrigger>
        </TabsList>
        <TabsContent value="details">Details panel</TabsContent>
        <TabsContent value="empty">Empty panel</TabsContent>
      </Tabs>,
    );

    await user.keyboard("{Tab}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Empty" })).toHaveAttribute(
      "data-state",
      "active",
    );
    expect(screen.getByRole("tab", { name: "Disabled" })).toBeDisabled();
  });
});
