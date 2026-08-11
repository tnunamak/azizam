import { axe } from "jest-axe";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui";

function DialogSmokeFixture() {
  const [open, setOpen] = useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>Open service details</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Service details</DialogTitle>
        <DialogDescription>
          Review the selected service configuration.
        </DialogDescription>
      </DialogContent>
    </Dialog>
  );
}

describe("Dialog", () => {
  it("opens by keyboard, closes with Escape, and has no axe violations", async () => {
    const user = userEvent.setup();
    render(<DialogSmokeFixture />);

    const trigger = screen.getByRole("button", {
      name: "Open service details",
    });
    await user.tab();
    expect(trigger).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(
      screen.getByRole("dialog", { name: "Service details" }),
    ).toBeVisible();

    const accessibility = await axe(document.body);
    expect(accessibility.violations).toEqual([]);

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
