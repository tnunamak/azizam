import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmDialog } from "@/components/ui";

function deferred() {
  let resolve: () => void;
  let reject: (reason?: unknown) => void;
  const promise = new Promise<void>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve: resolve!, reject: reject! };
}

function ConfirmFixture({ onConfirm }: { onConfirm: () => Promise<void> }) {
  const [open, setOpen] = useState(true);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title="Remove service?"
      description="Removal applies live."
      confirmLabel="Remove service"
      onConfirm={onConfirm}
    />
  );
}

describe("ConfirmDialog", () => {
  it("keeps destructive confirmation pending until the mutation succeeds", async () => {
    const user = userEvent.setup();
    const mutation = deferred();
    const onConfirm = vi.fn(() => mutation.promise);
    render(<ConfirmFixture onConfirm={onConfirm} />);

    const confirm = screen.getByRole("button", { name: "Remove service" });
    await user.click(confirm);

    expect(onConfirm).toHaveBeenCalledOnce();
    expect(confirm).toBeDisabled();
    mutation.resolve();

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Remove service?" }),
      ).toBeNull(),
    );
  });

  it("keeps failed destructive confirmation open for retry", async () => {
    const user = userEvent.setup();
    const mutation = deferred();
    render(<ConfirmFixture onConfirm={() => mutation.promise} />);

    const confirm = screen.getByRole("button", { name: "Remove service" });
    await user.click(confirm);
    expect(confirm).toBeDisabled();
    mutation.reject(new Error("remove failed"));

    await waitFor(() => expect(confirm).not.toBeDisabled());
    expect(
      screen.getByRole("dialog", { name: "Remove service?" }),
    ).toBeVisible();
  });
});
