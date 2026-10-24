import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelsBlock, draftsFromCheck, duplicateNames, kindRefusal, modelsBody, type ModelDraft } from "./models-block";

afterEach(() => cleanup());

const detected = (id: string, public_name = id) => ({ id, public_name, context_window: null, use: "Chat" });

const kinds = [
  { id: "chat", label: "Chat" },
  { id: "embed", label: "Embeddings" },
  { id: "speech", label: "Voice" },
  { id: "transcribe", label: "Transcription" },
];
const offeredKinds = ["chat", "embed"];

function Harness({ initial }: { initial: ModelDraft[] }) {
  const [models, setModels] = useState(initial);
  return <ModelsBlock models={models} onChange={setModels} emptyText="No models." kinds={kinds} offeredKinds={offeredKinds} />;
}

const capabilityOptions = (pick: HTMLElement) => within(pick).getAllByRole("option").map((option) => option.textContent);

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
    const names = screen.getAllByLabelText("Name apps use");
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

  it("every Context limit (tokens) field says what to do when the value is not known", () => {
    render(<Harness initial={draftsFromCheck([detected("a"), detected("b")])} />);
    const fields = screen.getAllByLabelText("Context limit (tokens)");
    expect(fields).toHaveLength(2);
    for (const field of fields) expect(field).toHaveAccessibleDescription("The model’s maximum input and output size in tokens. Leave empty if unknown.");
    expect(within(document.body).getAllByText("The model’s maximum input and output size in tokens. Leave empty if unknown.")).toHaveLength(2);
  });
});

describe("each model says what it is for", () => {
  it("a probed model's kind is pre-filled and saved with the model", () => {
    const models = draftsFromCheck([{ ...detected("nomic"), kind: "embed" }]);
    expect(models[0].kind).toBe("embed");
    expect(modelsBody(models)[0].kind).toBe("embed");
  });

  it("a model with no kind saves no kind, so every capability its engine offers still applies", () => {
    const models = draftsFromCheck([detected("alpha")]);
    expect(models[0].kind).toBe("");
    expect(modelsBody(models)[0]).not.toHaveProperty("kind");
  });

  it("the Capability pick-list offers the engine's kinds, and choosing one sets it", async () => {
    const user = userEvent.setup();
    render(<Harness initial={draftsFromCheck([detected("alpha")])} />);
    const pick = screen.getByLabelText("Capability");
    expect(pick.tagName).toBe("SELECT");
    expect(within(pick).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "All the engine offers",
      "Chat",
      "Embeddings",
    ]);
    expect(pick).toHaveValue("");
    await user.selectOptions(pick, "embed");
    expect(pick).toHaveValue("embed");
  });

  it("a read-only model shows its kind and cannot change it", () => {
    render(
      <ModelsBlock
        models={draftsFromCheck([{ ...detected("nomic"), kind: "embed" }])}
        onChange={() => undefined}
        emptyText="No models."
        kinds={kinds}
        offeredKinds={offeredKinds}
        readOnly
      />,
    );
    expect(screen.getByLabelText("Capability")).toHaveValue("embed");
    expect(screen.getByLabelText("Capability")).toBeDisabled();
  });

  it("the Capability pick-list offers only the kinds the engine offers, under their labels", () => {
    render(
      <ModelsBlock
        models={draftsFromCheck([detected("alpha")])}
        onChange={() => undefined}
        emptyText="No models."
        kinds={kinds}
        offeredKinds={["chat", "speech"]}
      />,
    );
    expect(capabilityOptions(screen.getByLabelText("Capability"))).toEqual(["All the engine offers", "Chat", "Voice"]);
  });

  it("a model keeps its kind in the pick-list when the engine no longer offers it, so the refusal can name it", () => {
    render(
      <ModelsBlock
        models={draftsFromCheck([{ ...detected("nomic"), kind: "embed" }])}
        onChange={() => undefined}
        emptyText="No models."
        kinds={kinds}
        offeredKinds={["chat"]}
      />,
    );
    expect(screen.getByLabelText("Capability")).toHaveValue("embed");
    expect(capabilityOptions(screen.getByLabelText("Capability"))).toEqual(["All the engine offers", "Chat", "Embeddings"]);
  });

  it("a refused kind shows its message under that model's Capability control and no other", () => {
    render(
      <ModelsBlock
        models={draftsFromCheck([{ ...detected("nomic"), kind: "embed" }, detected("qwen")])}
        onChange={() => undefined}
        emptyText="No models."
        kinds={kinds}
        offeredKinds={["chat"]}
        kindError={{ index: 0, message: "This engine doesn’t offer Embeddings." }}
      />,
    );
    const [first, second] = screen.getAllByLabelText("Capability");
    expect(first).toHaveAttribute("aria-invalid", "true");
    expect(first).toHaveAccessibleDescription(expect.stringContaining("This engine doesn’t offer Embeddings."));
    expect(second).not.toHaveAttribute("aria-invalid");
    expect(screen.getAllByText("This engine doesn’t offer Embeddings.")).toHaveLength(1);
  });
});

describe("the server's kind refusal", () => {
  it("is read from the field that names a model's kind, and is null for any other field", () => {
    expect(kindRefusal("models.2.kind", "This engine doesn’t offer Voice.")).toEqual({
      index: 2,
      message: "This engine doesn’t offer Voice.",
    });
    expect(kindRefusal("models", "Two models use the same name.")).toBeNull();
    expect(kindRefusal(null, "The gateway did not answer.")).toBeNull();
  });
});
