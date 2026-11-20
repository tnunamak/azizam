import { createElement, type ComponentProps, type KeyboardEvent } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkflowInputs, parseWorkflowInputs, workflowInputProblems,
  setWorkflowInputValue, readWorkflowInputValue,
  type WorkflowInput, type WorkflowInputMapping,
} from "./workflow-inputs";

const textarea = vi.hoisted(() => ({
  props: undefined as ComponentProps<"textarea"> | undefined,
}));

vi.mock("@/components/ui/textarea", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/components/ui/textarea")
  >();
  const React = await import("react");
  return {
    ...actual,
    Textarea: (props: ComponentProps<"textarea">) => {
      textarea.props = props;
      return React.createElement(actual.Textarea, props);
    },
  };
});

const select = vi.hoisted(() => ({
  change: undefined as ((id: string) => void) | undefined,
  options: new Map<string, string>(),
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({ children, onValueChange }: ComponentProps<"div"> & { onValueChange: (id: string) => void }) => {
    select.change = onValueChange;
    return children;
  },
  SelectTrigger: ({ children }: ComponentProps<"div">) => children,
  SelectContent: ({ children }: ComponentProps<"div">) => children,
  SelectValue: () => null,
  SelectItem: ({ children, value }: ComponentProps<"div"> & { value: string }) => {
    select.options.set(String(children), value);
    return children;
  },
}));

function selectChoice(input: WorkflowInput, value: unknown, label: string) {
  select.options.clear();
  select.change = undefined;
  const onChange = vi.fn();
  renderToStaticMarkup(createElement(WorkflowInputs, {
    inputs: [input], json: JSON.stringify({ choice: value }), onChange,
  }));
  const id = select.options.get(label);
  if (!id || !select.change) throw new Error("The select choice was not rendered.");
  select.change(id);
  expect(onChange).toHaveBeenCalledOnce();
  return JSON.parse(onChange.mock.calls[0][0]);
}

it("sends a declared reserved-looking choice and separately omits an optional choice", () => {
  const input: WorkflowInput = {
    name: "choice", type: "string", enum: ["__unset__", "a"], choice_labels: { __unset__: "Declared choice" },
  };
  expect(selectChoice(input, undefined, "Declared choice")).toEqual({ choice: "__unset__" });
  expect(selectChoice(input, "__unset__", "Not set")).toEqual({});
});

it("sends a numeric select choice without converting it to text", () => {
  const input: WorkflowInput = { name: "choice", type: "number", enum: [1, 2] };
  expect(selectChoice(input, 1, "2")).toEqual({ choice: 2 });
});

function renderPrompt(value = "") {
  const html = renderToStaticMarkup(
    createElement(WorkflowInputs, {
      inputs: [{ name: "prompt", type: "string", required: true }],
      json: JSON.stringify({ prompt: value }),
      onChange: vi.fn(),
      outputKind: "video",
    }),
  );
  const classes = html.match(/<textarea\b[^>]*\bclass="([^"]*)"/)?.[1];
  if (!textarea.props || classes === undefined) {
    throw new Error("The prompt textarea was not rendered.");
  }
  return { props: textarea.props, classes: classes.split(/\s+/) };
}

function expectGrowingPlaceholder(field: ReturnType<typeof renderPrompt>) {
  expect(field.props.placeholder).toBeTruthy();
  expect(field.props.value).toBe("");
  expect(field.classes).not.toContain("h-11");
  expect(field.classes).not.toContain("field-sizing-fixed");
  // One line of example keeps the compact height; only a wrapped example makes it taller.
  expect(field.classes).toContain("min-h-11");
}

beforeEach(() => {
  textarea.props = undefined;
});

describe("workflow prompt placeholder sizing", () => {
  it("leaves an empty field with a placeholder free to grow", () => {
    expectGrowingPlaceholder(renderPrompt());
  });

  it("becomes compact when the empty placeholder field gets a short value", () => {
    expectGrowingPlaceholder(renderPrompt());

    const field = renderPrompt("A sunrise over the sea");
    expect(field.props.value).toBe("A sunrise over the sea");
    expect(field.classes).toContain("h-11");
    expect(field.classes).toContain("field-sizing-fixed");
  });

  it("still submits on Enter after the placeholder field gets a short value", () => {
    expectGrowingPlaceholder(renderPrompt());

    const value = "A sunrise over the sea";
    const field = renderPrompt(value);
    const submit = vi.fn();
    const querySelector = vi.fn(() => ({ click: submit }));
    const preventDefault = vi.fn();

    field.props.onKeyDown?.({
      key: "Enter",
      nativeEvent: { isComposing: false },
      currentTarget: { value, form: { querySelector } },
      preventDefault,
    } as unknown as KeyboardEvent<HTMLTextAreaElement>);

    expect(preventDefault).toHaveBeenCalledOnce();
    expect(querySelector).toHaveBeenCalledWith('button[type="submit"]');
    expect(submit).toHaveBeenCalledOnce();
  });
});

describe("workflow inline validation matches submission", () => {
  const count: WorkflowInput = { name: "count", type: "integer", required: true, maximum: 10 };
  it.each([
    ['{"count":11}', "must be at most 10."],
    ['{"count":"12"}', "must be a number."],
  ])("reports the submit-time problem for %s", (json, message) => {
    const problems = workflowInputProblems(json, [count]);
    expect(problems.count).toContain(message);
    expect(() => parseWorkflowInputs(json, [count])).toThrow(problems.count);
  });
  it("reports an invalid enum with the same one-of message", () => {
    const input: WorkflowInput = { name: "choice", type: "string", enum: ["a", "b"] };
    const json = '{"choice":"c"}';
    const problems = workflowInputProblems(json, [input]);
    expect(problems.choice).toContain("must be one of: a, b.");
    expect(() => parseWorkflowInputs(json, [input])).toThrow(problems.choice);
  });
  it("keeps valid values and optional empty values free of problems", () => {
    expect(workflowInputProblems('{"count":10}', [count])).toEqual({});
    expect(() => parseWorkflowInputs('{"count":10}', [count])).not.toThrow();
    expect(workflowInputProblems("{}", [{ ...count, required: false }])).toEqual({});
  });
  it("rejects a pasted integer that JSON rounds outside the safe range", () => {
    const inputs: WorkflowInput[] = [{ name: "seed", type: "integer" }];
    // Keep the original pasted digits, rather than stringify an already-rounded JS number.
    const json = '{"seed":9007199254740993}';
    const problems = workflowInputProblems(json, inputs);
    expect(problems.seed).toContain("is too large to send exactly.");
    expect(() => parseWorkflowInputs(json, inputs)).toThrow(problems.seed);
    for (const seed of [Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER]) {
      const safe = JSON.stringify({ seed });
      expect(() => parseWorkflowInputs(safe, inputs)).not.toThrow();
      expect(workflowInputProblems(safe, inputs)).toEqual({});
    }
    expect(() => parseWorkflowInputs('{"seed":1.5}', inputs)).toThrow("must be a whole number.");
  });
  it("continues skipping ignored, unaccepted and unsupported combined inputs", () => {
    const inputs: WorkflowInput[] = [
      { name: "ignored", type: "integer", ignored: true },
      { name: "unaccepted", type: "integer", accepted: false },
      { name: "combined", type: "integer" },
    ];
    const mapping: WorkflowInputMapping = { combined: { field: "unsupported", combine: ["combined", "other"] } };
    expect(workflowInputProblems('{"ignored":"bad","unaccepted":"bad","unsupported":"bad"}', inputs, mapping)).toEqual({});
  });
});

describe("workflow combined size parts", () => {
  for (const names of [["w", "h"], ["width", "height"]]) {
    for (const explicit of [false, true]) {
      it(`round trips ${names.join("/")} using ${explicit ? "declared" : "shared"} parts`, () => {
        const inputs: WorkflowInput[] = names.map(name => ({
          name, type: "integer", request_field: "size", default: 256,
        }));
        const mapping: WorkflowInputMapping = explicit ? Object.fromEntries(names.map(name => [
          name, { field: "size", combine: names },
        ])) : {};
        const values: Record<string, unknown> = {};
        setWorkflowInputValue(values, inputs, inputs[0], 512, mapping);
        setWorkflowInputValue(values, inputs, inputs[1], 768, mapping);
        expect(values.size).toBe("512x768");
        expect(readWorkflowInputValue(values, inputs, inputs[0], mapping)).toBe(512);
        expect(readWorkflowInputValue(values, inputs, inputs[1], mapping)).toBe(768);
        expect(workflowInputProblems(JSON.stringify(values), inputs, mapping)).toEqual({});
        expect(() => parseWorkflowInputs(JSON.stringify(values), inputs, mapping)).not.toThrow();
      });
    }
  }
  it("uses declared order rather than literal width/height names", () => {
    const inputs: WorkflowInput[] = [{ name: "height", type: "integer" }, { name: "width", type: "integer" }];
    const mapping: WorkflowInputMapping = Object.fromEntries(inputs.map(input => [
      input.name, { field: "size", combine: ["height", "width"] },
    ]));
    expect(readWorkflowInputValue({ size: "512x768" }, inputs, inputs[0], mapping)).toBe(512);
    expect(readWorkflowInputValue({ size: "512x768" }, inputs, inputs[1], mapping)).toBe(768);
  });
  it("preserves the value for a name outside the declared parts", () => {
    const input: WorkflowInput = { name: "other", type: "string" };
    expect(readWorkflowInputValue({ size: "512x768" }, [input], input, {
      other: { field: "size", combine: ["w", "h"] },
    })).toBe("512x768");
  });
});
