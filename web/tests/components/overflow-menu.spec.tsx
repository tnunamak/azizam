import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OverflowMenu, OverflowMenuItem } from "@/components/ui/overflow-menu";

function renderMenu(onRename = vi.fn()) {
  render(
    <OverflowMenu label="More actions">
      <OverflowMenuItem onSelect={onRename}>Rename</OverflowMenuItem>
      <OverflowMenuItem disabled>Archive</OverflowMenuItem>
      <OverflowMenuItem danger>Remove</OverflowMenuItem>
    </OverflowMenu>,
  );
  return onRename;
}

afterEach(cleanup);

describe("OverflowMenu", () => {
  it("shows which normal item has keyboard focus", async () => {
    const user = userEvent.setup();
    renderMenu();
    await user.click(screen.getByRole("button", { name: "More actions" }));
    await user.keyboard("{ArrowDown}");
    const item = screen.getByRole("menuitem", { name: "Rename" });
    expect(item).toHaveAttribute("data-highlighted");
    expect(item.className).toContain("data-[highlighted]:bg-accent");
  });

  it("skips the disabled item and highlights the danger item", async () => {
    const user = userEvent.setup();
    renderMenu();
    await user.click(screen.getByRole("button", { name: "More actions" }));
    await user.keyboard("{ArrowDown}{ArrowDown}");
    const archive = screen.getByRole("menuitem", { name: "Archive" });
    const remove = screen.getByRole("menuitem", { name: "Remove" });
    expect(archive).toHaveAttribute("data-disabled");
    expect(archive).not.toHaveAttribute("data-highlighted");
    expect(remove).toHaveAttribute("data-highlighted");
    expect(remove.className).toContain("data-[highlighted]:bg-danger/15");
  });

  it("still activates the focused item with Enter", async () => {
    const user = userEvent.setup();
    const onRename = renderMenu();
    await user.click(screen.getByRole("button", { name: "More actions" }));
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onRename).toHaveBeenCalledTimes(1);
  });
});
