import { createElement, type ComponentProps, type KeyboardEvent } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkflowInputs, parseWorkflowInputs, workflowDefaults, workflowInputProblems,
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
  selected: undefined as string | undefined,
  options: [] as { label: string; id: string }[],
  known: [] as { label: string; id: string }[],
  shown: undefined as string | undefined,
}));

vi.mock("@/components/ui/select", () => ({
  Select: ({ children, onValueChange, value }: ComponentProps<"div"> & {
    onValueChange: (id: string) => void; value: string;
  }) => {
    select.change = onValueChange;
    select.selected = value;
    return children;
  },
  SelectTrigger: ({ children }: ComponentProps<"div">) => children,
  SelectContent: ({ children }: ComponentProps<"div">) => children,
  SelectValue: () => {
    select.shown = select.known.find(option => option.id === select.selected)?.label;
    return null;
  },
  SelectItem: ({ children, value }: ComponentProps<"div"> & { value: string }) => {
    select.options.push({ label: String(children), id: value });
    return children;
  },
}));

function renderSelect(input: WorkflowInput, body: object) {
  const onChange = vi.fn();
  const render = () => {
    select.options.length = 0;
    select.change = undefined;
    select.selected = undefined;
    select.shown = undefined;
    renderToStaticMarkup(createElement(WorkflowInputs, {
      inputs: [input], json: JSON.stringify(body), onChange,
    }));
  };
  // The trigger renders before the options do; a second pass lets it look its label up.
  select.known = [];
  render();
  select.known = [...select.options];
  render();
  return {
    options: [...select.options],
    selected: select.selected,
    shown: select.shown,
    choose(id: string) {
      if (!select.change) throw new Error("The select control was not rendered.");
      select.change(id);
      expect(onChange).toHaveBeenCalledOnce();
      return JSON.parse(onChange.mock.calls[0][0]);
    },
  };
}

function selectChoice(input: WorkflowInput, value: unknown, label: string) {
  const control = renderSelect(input, { choice: value });
  const id = control.options.find(option => option.label === label)?.id;
  if (!id) throw new Error("The select choice was not rendered.");
  return control.choose(id);
}

it("round-trips both reserved-looking enum values and clears only with Not set", () => {
  const input: WorkflowInput = { name: "choice", type: "string", enum: ["unset", "__unset__"] };
  for (const declared of ["unset", "__unset__"]) {
    const initial = renderSelect(input, {});
    const unset = initial.options.find(option => option.label === "Not set")!;
    const choice = initial.options.find(option => option.label === declared)!;
    expect(choice.id).not.toBe(unset.id);
    expect(initial.selected).toBe(unset.id);
    expect(initial.shown).toBe("Not set");
    const body = initial.choose(choice.id);
    expect(body).toEqual({ choice: declared });
    const chosen = renderSelect(input, body);
    expect(chosen.selected).toBe(choice.id);
    expect(chosen.shown).toBe(declared);
    const cleared = chosen.choose(unset.id);
    expect(cleared).toEqual({});
    expect(renderSelect(input, cleared).selected).toBe(unset.id);
  }
});

it("shows distinct duplicate display labels without changing their wire values", () => {
  const input: WorkflowInput = {
    name: "choice", type: "string", enum: ["first", "second"],
    choice_labels: { first: "Same label", second: "Same label" },
  };
  for (const [index, value] of ["first", "second"].entries()) {
    const control = renderSelect(input, {});
    const options = control.options.filter(option => option.label.startsWith("Same label"));
    expect(options.map(option => option.label)).toEqual(["Same label (first)", "Same label (second)"]);
    expect(options[0].id).not.toBe(options[1].id);
    const body = control.choose(options[index].id);
    expect(body).toEqual({ choice: value });
    const chosen = renderSelect(input, body);
    expect(chosen.selected).toBe(options[index].id);
    expect(chosen.shown).toBe(options[index].label);
  }
});

it("annotates only collisions, leaving a third unique label alone", () => {
  const input: WorkflowInput = {
    name: "choice", type: "string", enum: ["first", "second", "third"],
    choice_labels: { first: "Same label", second: "Same label", third: "Unique label" },
  };
  const control = renderSelect(input, {});
  expect(control.options.map(option => option.label)).toEqual([
    "Not set", "Same label (first)", "Same label (second)", "Unique label",
  ]);
  expect(control.choose(control.options[3].id)).toEqual({ choice: "third" });
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

describe("workflow starting values", () => {
  const fixed: WorkflowInput = { name: "mode", type: "string", required: true, enum: ["standard"] };

  it("seeds the missing required singleton from a valid catalog without an example", () => {
    expect(workflowDefaults([fixed], {})).toEqual({ mode: "standard" });
    expect(workflowDefaults([fixed], { mode: "standard" })).toEqual({ mode: "standard" });
  });

  const extensionMapping: WorkflowInputMapping = { mode: { in: "extension", field: "fixed_mode" } };

  it("seeds a missing fixed choice only in its mapped workflow extension", () => {
    expect(workflowDefaults([fixed], {}, extensionMapping)).toEqual({
      extensions: { "com.azizam.workflow": { fixed_mode: "standard" } },
    });
  });

  it("leaves an existing mapped fixed choice in its original extension envelope", () => {
    const example = { extensions: '{ "com.azizam.workflow": { "fixed_mode": "standard" } }' };
    expect(workflowDefaults([fixed], example, extensionMapping)).toEqual(example);
  });

  it("writes only inside the request when the example wraps the body", () => {
    // An envelope makes request, not the outer object, the mapped write target.
    expect(workflowDefaults([fixed], { request: {} }, extensionMapping)).toEqual({
      request: { extensions: { "com.azizam.workflow": { fixed_mode: "standard" } } },
    });
  });

  it("does not seed an optional, ignored, or unaccepted singleton", () => {
    for (const input of [{ ...fixed, required: false }, { ...fixed, ignored: true }, { ...fixed, accepted: false }]) {
      expect(workflowDefaults([input], {})).toEqual({});
    }
  });
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

describe("guided extensions JSON validation", () => {
  it.each(["[]", "null", "42", '"text"'])("reports valid non-object JSON as the wrong shape: %s", (extensions) => {
    expect(() => parseWorkflowInputs(JSON.stringify({ extensions }))).toThrow(
      "extensions must be a JSON object.",
    );
  });

  it("distinguishes malformed JSON from an accepted object string", () => {
    expect(() => parseWorkflowInputs(JSON.stringify({ extensions: "{" }))).toThrow(
      "extensions must be valid JSON before using guided controls.",
    );
    const value = { extensions: '{"metadata":true}' };
    expect(parseWorkflowInputs(JSON.stringify(value))).toEqual(value);
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
