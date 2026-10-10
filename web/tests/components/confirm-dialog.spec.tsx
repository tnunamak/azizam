import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";

afterEach(cleanup);

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

it("requires the exact typed name, blocks dismissal while pending, and resets on reopen", async () => {
  const user = userEvent.setup();
  const mutation = deferred();
  const onConfirm = vi.fn(() => mutation.promise);
  function TypedFixture() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button onClick={() => setOpen(true)}>Open removal</button>
        <ConfirmDialog
          open={open}
          onOpenChange={setOpen}
          title="Remove engine?"
          description="Removal applies live."
          confirmLabel="Remove engine"
          typedName="my-engine"
          onConfirm={onConfirm}
        />
      </>
    );
  }
  render(<TypedFixture />);
  const trigger = screen.getByRole("button", { name: "Open removal" });
  await user.click(trigger);
  const input = screen.getByRole("textbox", { name: /Type my-engine to confirm/ });
  const confirm = screen.getByRole("button", { name: "Remove engine" });
  expect(input).toHaveFocus();
  expect(confirm).toBeDisabled();
  await user.type(input, "My-engine");
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "my-engine ");
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "my-engine");
  await user.click(confirm);
  expect(confirm).toBeDisabled();
  expect(confirm).toHaveAttribute("aria-busy", "true");
  expect(input).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "Remove engine?" })).toBeVisible();
  expect(onConfirm).toHaveBeenCalledOnce();
  mutation.resolve();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await waitFor(() => expect(trigger).toHaveFocus());
  await user.click(trigger);
  expect(screen.getByRole("textbox", { name: /Type my-engine to confirm/ })).toHaveValue("");
  expect(screen.getByRole("button", { name: "Remove engine" })).toBeDisabled();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(trigger).toHaveFocus());
});
