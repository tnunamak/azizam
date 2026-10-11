import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
afterEach(cleanup);
describe("confirmation dialog behavior", () => {
  it("consumes destructive rejection and keeps the dialog target mounted", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockRejectedValue(new Error("nope"));
    const onOpenChange = vi.fn();
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);

    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Disable app?"
        description="Fixture app stays targeted."
        confirmLabel="Disable app"
        onConfirm={onConfirm}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Disable app" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(screen.getByRole("dialog", { name: "Disable app?" })).toBeVisible();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });
});
