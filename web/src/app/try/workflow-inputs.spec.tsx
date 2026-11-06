import { createElement, type ComponentProps, type KeyboardEvent } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowInputs } from "./workflow-inputs";

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
