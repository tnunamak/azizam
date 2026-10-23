import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SecretReveal } from "./secret-reveal";
import { unsavedSecretEntry, unsavedWorkNow } from "@/lib/unsaved-work";

it("holds a shown secret until its person saves it", () => {
  render(<SecretReveal secret="test-secret-not-real" />);
  expect(unsavedWorkNow()).toEqual([expect.objectContaining({ kind: "new-key", count: 1 })]);
  expect(unsavedSecretEntry()).not.toBeNull();
});

it("has released the hold before the save is reported, so the save's own navigation is not held", () => {
  let heldWhenSaved: boolean | null = null;
  render(
    <SecretReveal
      secret="test-secret-not-real"
      onSaved={() => {
        heldWhenSaved = unsavedSecretEntry() !== null;
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "I’ve saved it" }));
  expect(heldWhenSaved).toBe(false);
  expect(unsavedWorkNow()).toEqual([]);
});

it("holds nothing when there is no secret to show", () => {
  render(<SecretReveal secret={null} />);
  expect(unsavedWorkNow()).toEqual([]);
});
