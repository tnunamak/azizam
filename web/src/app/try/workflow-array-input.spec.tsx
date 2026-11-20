import { createElement, type ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, expect, it, vi } from "vitest";
import { WorkflowInputs, type WorkflowInput } from "./workflow-inputs";

const controls = vi.hoisted(() => ({
  buttons: new Map<string, ComponentProps<"button">>(),
  numbers: new Map<string, ComponentProps<"input">>(),
  choices: new Map<string, { value?: string; change?: (value: string) => void }>(),
  options: [] as { value: string; label: string }[],
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
    SelectItem: (props: { value?: string; children?: unknown }) => {
      controls.options.push({ value: props.value ?? "", label: String(props.children ?? "") });
      return null;
    },
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

const labeled: WorkflowInput = {
  ...array,
  items: { type: "object", properties: {
    name: {
      type: "string", label: "Name",
      enum: ["ast_1", "b.safetensors"],
      choice_labels: { ast_1: "My watercolour", "not-a-choice": "Ignored", "b.safetensors": "" },
    },
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
    controls.options.length = 0;
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
  controls.options.length = 0;
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

it("lists readable names for an item field's choices", () => {
  const { render } = view(labeled);
  render();
  click("Add Styles");
  render();
  expect(controls.options.map((option) => option.label)).toEqual([
    "Not set",
    "My watercolour",
    "b.safetensors",
  ]);
});

it("sends the value, never the label, when a readable name is chosen", () => {
  const { render, onChange } = view(labeled);
  render();
  click("Add Styles");
  render();
  const mine = controls.options.find((option) => option.label === "My watercolour");
  expect(mine?.value).toBe("ast_1");
  controls.choices.get("Name, row 1")?.change?.(mine!.value);
  render();
  expect(JSON.parse(onChange.mock.lastCall![0])).toEqual({
    other: true, styles: [{ name: "ast_1", weight: 1 }],
  });
});

it("ignores a label key that is not a choice and falls back for an empty label", () => {
  const { render } = view(labeled);
  render();
  click("Add Styles");
  render();
  const labels = controls.options.map((option) => option.label);
  expect(labels).not.toContain("Ignored");
  expect(labels).toContain("b.safetensors");
});
