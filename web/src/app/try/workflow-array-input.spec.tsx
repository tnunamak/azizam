// @vitest-environment jsdom
import { act, createElement, useState, type ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, expect, it, vi } from "vitest";
import { WorkflowInputs, type WorkflowInput } from "./workflow-inputs";

const controls = vi.hoisted(() => ({
  buttons: new Map<string, ComponentProps<"button">>(),
  numbers: new Map<string, ComponentProps<"input">>(),
  choices: new Map<string, { value?: string; change?: (value: string) => void }>(),
}));

vi.mock("@/components/ui/button", () => ({
  Button: (props: ComponentProps<"button">) => {
    if (props["aria-label"]) controls.buttons.set(props["aria-label"], props);
    return createElement("button", props);
  },
}));
vi.mock("@/components/ui/input", () => ({
  Input: (props: ComponentProps<"input">) => {
    if (props.type === "number" && props["aria-label"]) controls.numbers.set(props["aria-label"], props);
    return createElement("input", props);
  },
}));
vi.mock("@/components/ui/select", async () => {
  const { createContext, useContext } = await import("react");
  const model = createContext<{ value?: string; onValueChange?: (value: string) => void }>({});
  return {
    Select: ({ children, ...props }: { children: React.ReactNode; value?: string; onValueChange?: (value: string) => void }) =>
      createElement(model.Provider, { value: props }, children),
    SelectTrigger: (props: ComponentProps<"button">) => {
      const current = useContext(model);
      if (props["aria-label"]) controls.choices.set(props["aria-label"], {
        value: current.value,
        change: current.onValueChange,
      });
      return createElement("button", { ...props, role: "combobox" });
    },
    SelectValue: ({ placeholder }: { placeholder: string }) => createElement("span", null, placeholder),
    SelectContent: () => null,
    SelectItem: () => null,
  };
});

const array: WorkflowInput = {
  name: "styles",
  label: "Styles",
  type: "array",
  items: { type: "object", properties: {
    name: { type: "string", label: "Name", enum: ["a.safetensors", "b.safetensors"] },
    weight: { type: "number", label: "Weight", minimum: -4, maximum: 4, default: 1 },
  } },
};

function view(input: WorkflowInput) {
  let json = '{"other":true}';
  const onChange = vi.fn((next: string) => { json = next; });
  const render = () => {
    controls.buttons.clear();
    controls.numbers.clear();
    controls.choices.clear();
    return renderToStaticMarkup(createElement(WorkflowInputs, { inputs: [input], json, onChange }));
  };
  return { render, onChange };
}

function click(name: string) {
  const button = controls.buttons.get(name);
  expect(button).toBeDefined();
  expect(button?.disabled).toBeFalsy();
  button?.onClick?.({} as Parameters<NonNullable<ComponentProps<"button">["onClick"]>>[0]);
}

beforeEach(() => {
  controls.buttons.clear();
  controls.numbers.clear();
  controls.choices.clear();
});

it("adds a defaulted row, names every control, edits the JSON, and removes the final key", () => {
  const { render, onChange } = view(array);
  render();
  click("Add Styles");
  render();
  expect(controls.choices.get("Name, row 1")?.value).toBe("");
  const weight = controls.numbers.get("Weight, row 1");
  expect(weight).toMatchObject({ value: "1", min: -4, max: 4 });
  controls.choices.get("Name, row 1")?.change?.("b.safetensors");
  render();
  const change = controls.numbers.get("Weight, row 1")?.onChange;
  change?.({ currentTarget: { value: "0.5" } } as Parameters<NonNullable<typeof change>>[0]);
  expect(JSON.parse(onChange.mock.lastCall![0])).toEqual({
    other: true, styles: [{ name: "b.safetensors", weight: 0.5 }],
  });
  render();
  click("Remove Styles row 1");
  expect(JSON.parse(onChange.mock.lastCall![0])).toEqual({ other: true });
});

it("disables Add at the maximum and names the second row", () => {
  const { render } = view({ ...array, max_items: 2 });
  render();
  click("Add Styles");
  render();
  click("Add Styles");
  expect(render()).toContain("You can add up to 2 items.");
  expect(controls.buttons.get("Add Styles")?.disabled).toBe(true);
  expect(controls.buttons.has("Remove Styles row 2")).toBe(true);
  expect(controls.choices.has("Name, row 2")).toBe(true);
  expect(controls.numbers.has("Weight, row 2")).toBe(true);
});

it("does not offer Add when no items are available for this model", () => {
  expect(view({ ...array, max_items: 0 }).render()).toContain("No items are available for this model.");
  expect(controls.buttons.has("Add Styles")).toBe(false);
});

it.each([
  undefined,
  { type: "object", properties: { nested: { type: "object" } } },
])("keeps the unloaded notice for an unsupported array schema", (items) => {
  expect(view({ ...array, items }).render()).toContain("inputs could not be loaded");
  expect(controls.buttons.has("Add Styles")).toBe(false);
});

it("focuses the added row at the maximum and keeps focus and identity after removal", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const env = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previous = env.IS_REACT_ACT_ENVIRONMENT;
  env.IS_REACT_ACT_ENVIRONMENT = true;
  function Harness() {
    const [json, setJson] = useState("{}");
    return createElement(WorkflowInputs, { inputs: [{ ...array, max_items: 3 }], json, onChange: setJson });
  }
  const add = () => host.querySelector<HTMLButtonElement>('[aria-label="Add Styles"]')!;
  const rows = () => [...host.querySelectorAll<HTMLFieldSetElement>("fieldset")];
  const remove = async (number: number) => {
    const button = host.querySelector<HTMLButtonElement>(`[aria-label="Remove Styles row ${number}"]`)!;
    button.focus();
    await act(async () => button.click());
  };
  try {
    await act(async () => root.render(createElement(Harness)));
    add().focus();
    for (let i = 0; i < 3; i++) await act(async () => add().click());
    expect(add().disabled).toBe(true);
    const [first, , third] = rows();
    expect(document.activeElement).toBe(third);
    await remove(2);
    expect(rows()[1]).toBe(third);
    expect(document.activeElement).toBe(third);
    await remove(2);
    expect(document.activeElement).toBe(first);
    await remove(1);
    expect(document.activeElement).toBe(add());
  } finally {
    await act(async () => root.unmount());
    host.remove();
    env.IS_REACT_ACT_ENVIRONMENT = previous;
  }
});
