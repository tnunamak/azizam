import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OverflowMenu, OverflowMenuItem } from "@/components/ui/overflow-menu";

describe("OverflowMenu", () => {
  it("shows which item has keyboard focus", async () => {
    const user = userEvent.setup();
    render(
      <OverflowMenu label="More actions">
        <OverflowMenuItem>Rename</OverflowMenuItem>
        <OverflowMenuItem danger>Remove</OverflowMenuItem>
      </OverflowMenu>,
    );
    await user.click(screen.getByRole("button", { name: "More actions" }));
    await user.keyboard("{ArrowDown}");
    const item = screen.getByRole("menuitem", { name: "Rename" });
    expect(item).toHaveAttribute("data-highlighted");
    expect(item.className).toContain("data-[highlighted]:bg-accent");
  });
});
