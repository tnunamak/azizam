import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import NotFound from "@/app/not-found";
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

describe("not-found recovery", () => {
  it("explains the missing page and how to recover in plain English", () => {
    render(<NotFound />);

    expect(screen.getByRole("heading", { name: "Page not found" })).toBeVisible();
    expect(screen.getByText("We couldn't find a page at this address. It may have moved or been removed. Check the address for a typo, or go Home to find what you need.")).toBeVisible();
  });

  it("offers a Home link instead of retrying an unavailable route", () => {
    render(<NotFound />);

    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: /retry|try again/i })).not.toBeInTheDocument();
  });

  it("does not echo potentially sensitive details from the missing address", () => {
    const originalUrl = window.location.href;
    try {
      window.history.replaceState({}, "", "/missing-private-route?error=RAW_PRIVATE_ERROR");
      const { container } = render(<NotFound />);
      expect(container).not.toHaveTextContent(/RAW_PRIVATE_ERROR|missing-private-route/);
    } finally {
      window.history.replaceState({}, "", originalUrl);
    }
  });
});
