import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelsBlock, draftsFromCheck, duplicateNames, modelsBody, type ModelDraft } from "./models-block";

afterEach(() => cleanup());

const detected = (id: string, public_name = id) => ({ id, public_name, context_window: null, use: "Chat" });

function Harness({ initial }: { initial: ModelDraft[] }) {
  const [models, setModels] = useState(initial);
  return <ModelsBlock models={models} onChange={setModels} emptyText="No models." />;
}

describe("Add a model by name says why nothing happened", () => {
  it("an empty name asks for the model name used by the server", async () => {
    const user = userEvent.setup();
    render(<Harness initial={draftsFromCheck([detected("alpha")])} />);
    await user.click(screen.getByRole("button", { name: "Add model" }));
    const input = screen.getByLabelText("Add a model by name");
    expect(screen.getByText("Enter the model name used by the server.")).toBeVisible();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(expect.stringContaining("Enter the model name used by the server."));
    expect(screen.getByText("1 of 1 models offered")).toBeVisible();
  });

  it("a name that is already listed says where to find it", async () => {
    const user = userEvent.setup();
    render(<Harness initial={draftsFromCheck([detected("alpha")])} />);
    await user.type(screen.getByLabelText("Add a model by name"), "alpha");
    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(screen.getByText("This model is already listed. Find it above and tick it to offer it.")).toBeVisible();
    expect(screen.getByText("1 of 1 models offered")).toBeVisible();
  });

  it("the message goes away when the owner types again, and a new name is added", async () => {
    const user = userEvent.setup();
    render(<Harness initial={draftsFromCheck([detected("alpha")])} />);
    await user.click(screen.getByRole("button", { name: "Add model" }));
    await user.type(screen.getByLabelText("Add a model by name"), "beta");
    expect(screen.queryByText("Enter the model name used by the server.")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(screen.getByText("2 of 2 models offered")).toBeVisible();
    expect(screen.getByLabelText("Add a model by name")).not.toHaveAttribute("aria-invalid");
  });
});

describe("duplicate public names use the name that is submitted", () => {
  it("two distinct ids with empty public names do not block each other", () => {
    const models = draftsFromCheck([detected("a", ""), detected("b", "")]);
    expect(duplicateNames(models).size).toBe(0);
    expect(modelsBody(models).map((m) => m.public_name)).toEqual(["a", "b"]);
  });

  it("an empty public name collides with another model that is named the same as its id", () => {
    const models = draftsFromCheck([detected("x", ""), detected("y", "x")]);
    expect([...duplicateNames(models)]).toEqual(["x"]);
    expect(new Set(modelsBody(models).map((m) => m.public_name)).size).toBe(1);
  });

  it("the field error names the effective name and marks both fields", () => {
    render(<Harness initial={draftsFromCheck([detected("x", ""), detected("y", "x")])} />);
    const names = screen.getAllByLabelText("Public name");
    expect(names[0]).toHaveAttribute("aria-invalid", "true");
    expect(names[1]).toHaveAttribute("aria-invalid", "true");
    expect(screen.getAllByText("Two models use the public name “x”. Each needs its own.")).toHaveLength(2);
  });

  it("a model that is off does not count", () => {
    const models = draftsFromCheck([detected("x", ""), detected("y", "x")]);
    models[1].on = false;
    expect(duplicateNames(models).size).toBe(0);
  });
});

describe("the model list on a phone", () => {
  it("Remove model has a short visible label, the model in its accessible name and a 44 px target", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[]} />);
    await user.type(screen.getByLabelText("Add a model by name"), "my-very-long-model-name-q4_k_m");
    await user.click(screen.getByRole("button", { name: "Add model" }));
    const remove = screen.getByRole("button", { name: "Remove model my-very-long-model-name-q4_k_m" });
    expect(remove.childNodes[0].textContent).toBe("Remove model");
    expect(remove).toHaveClass("max-sm:min-h-11");
    await user.click(remove);
    expect(screen.queryByRole("button", { name: /^Remove model/ })).toBeNull();
  });

  it("every Context window field says what to do when the value is not known", () => {
    render(<Harness initial={draftsFromCheck([detected("a"), detected("b")])} />);
    const fields = screen.getAllByLabelText("Context window");
    expect(fields).toHaveLength(2);
    for (const field of fields) expect(field).toHaveAccessibleDescription("Leave empty if you don’t know.");
    expect(within(document.body).getAllByText("Leave empty if you don’t know.")).toHaveLength(2);
  });
});
