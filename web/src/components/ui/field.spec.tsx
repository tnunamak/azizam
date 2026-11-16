import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Field } from "./field";
import { Textarea } from "./textarea";
import { Checkbox } from "./checkbox";
import { Switch } from "./switch";

describe.each([
  { name: "Textarea", Control: Textarea, role: "textbox", tag: "TEXTAREA" },
  { name: "Checkbox", Control: Checkbox, role: "checkbox", tag: "BUTTON" },
  { name: "Switch", Control: Switch, role: "switch", tag: "BUTTON" },
])("$name Field wiring", ({ Control, role, tag }) => {
  it("links the generated label and help to the actual control", () => {
    render(<Field label="Notes" help="Optional"><Control /></Field>);
    const control = screen.getByLabelText("Notes");
    expect(control).toBe(screen.getByRole(role));
    expect(control.tagName).toBe(tag);
    expect(control.id).not.toBe("");
    expect(control).toHaveAccessibleDescription("Optional");
    expect(control).not.toHaveAttribute("aria-invalid");
  });

  it("replaces help with the error and marks the control invalid", () => {
    render(<Field label="Notes" help="Optional" error="Too long"><Control aria-invalid={false} /></Field>);
    const control = screen.getByLabelText("Notes");
    expect(control).toHaveAttribute("aria-invalid", "true");
    expect(control).toHaveAccessibleDescription("Too long");
    expect(screen.queryByText("Optional")).not.toBeInTheDocument();
  });

  it("preserves caller id and description over context defaults", () => {
    render(<>
      <p id="caller-help">Caller description</p>
      <Field label="Notes" help="Optional">
        <Control id="caller-control" aria-describedby="caller-help" />
      </Field>
    </>);
    const control = screen.getByRole(role);
    expect(control).toHaveAttribute("id", "caller-control");
    expect(control).toHaveAttribute("aria-describedby", "caller-help");
    expect(control).toHaveAccessibleDescription("Caller description");
  });

  it("does not invent an id or description outside a Field", () => {
    render(<Control />);
    const control = screen.getByRole(role);
    expect(control).not.toHaveAttribute("id");
    expect(control).not.toHaveAttribute("aria-describedby");
    expect(control).not.toHaveAttribute("aria-invalid");
  });

  it.each([true, "true"] as const)("preserves caller invalid=%s", (invalid) => {
    render(<Control aria-invalid={invalid} />);
    expect(screen.getByRole(role)).toHaveAttribute("aria-invalid", "true");
  });
});
