import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Button as ModernButton } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { CopyButton, copyText } from "@/components/ui/copy-button";
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

  it("preserves migrated button keyboard activation, links, and touch targets", async () => {
    const user = userEvent.setup();
    const onButtonClick = vi.fn();
    const onIconClick = vi.fn();
    render(
      <>
        <ModernButton size="sm" onClick={onButtonClick}>
          Test
        </ModernButton>
        <ModernButton asChild size="sm">
          <a href="#edit">Edit</a>
        </ModernButton>
        <IconButton size="sm" aria-label="Close" onClick={onIconClick}>
          x
        </IconButton>
      </>,
    );

    await user.keyboard("{Tab}{Enter}");
    await user.keyboard("{Tab}{Tab}{Enter}");
    expect(onButtonClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Test" })).toHaveClass("max-sm:min-h-11", "max-sm:min-w-11");
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

it.each(["missing", "rejected"] as const)("copies with the textarea fallback when clipboard is %s", async (mode) => {
  const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const execCommand = Object.getOwnPropertyDescriptor(document, "execCommand");
  const copy = vi.fn(() => true);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: mode === "missing" ? undefined : { writeText: vi.fn().mockRejectedValue(new Error("Unavailable")) },
  });
  Object.defineProperty(document, "execCommand", { configurable: true, value: copy });
  try {
    expect(await copyText("example")).toBe(true);
    expect(copy).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
  } finally {
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
    if (execCommand) Object.defineProperty(document, "execCommand", execCommand);
    else Reflect.deleteProperty(document, "execCommand");
  }
});

it("keeps Copy accessible and non-submitting inside a form", async () => {
  const user = userEvent.setup();
  const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
  render(<form onSubmit={submit}><CopyButton value="example" /></form>);
  const copy = screen.getByRole("button", { name: "Copy" });
  expect(copy).toHaveClass("max-sm:min-h-11", "max-sm:min-w-11");
  await user.click(copy);
  expect(submit).not.toHaveBeenCalled();
});
