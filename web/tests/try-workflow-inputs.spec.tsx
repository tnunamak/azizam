import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  WorkflowInputs,
  parseWorkflowInputs,
  readWorkflowInputValue,
  setWorkflowInputValue,
  workflowDefaults,
  type WorkflowInputMapping,
  type WorkflowInput,
} from "@/app/try/workflow-inputs";

const inputs: WorkflowInput[] = [
  {
    name: "prompt",
    type: "string",
    request_field: "text",
    required: true,
    default: "stone arch",
  },
  {
    name: "steps",
    type: "integer",
    minimum: 1,
    maximum: 50,
    default: 8,
  },
  {
    name: "guidance",
    type: "number",
    minimum: 0,
    maximum: 10,
  },
  {
    name: "transparent_background",
    type: "boolean",
    default: false,
  },
  {
    name: "size",
    type: "string",
    enum: ["512x512", "1024x1024"],
    default: "1024x1024",
  },
  {
    name: "source_image",
    type: "asset",
    required: true,
  },
  {
    name: "seed",
    type: "integer",
    ignored: true,
    default: 0,
  },
];

const mapping: WorkflowInputMapping = {
  prompt: { in: "body", field: "prompt" },
  width: { in: "body", field: "size", combine: ["width", "height"] },
  height: { in: "body", field: "size", combine: ["width", "height"] },
  steps: { in: "extension", field: "steps" },
};

const mappedInputs: WorkflowInput[] = [
  { name: "prompt", type: "string", required: true },
  { name: "width", type: "integer", default: 1024, request_field: "size" },
  { name: "height", type: "integer", default: 768, request_field: "size" },
  {
    name: "steps",
    type: "integer",
    default: 8,
    applies: "Workflow extension",
  },
];

afterEach(cleanup);

describe("workflow input helpers", () => {
  it("builds defaults without replacing example or extra fields", () => {
    expect(
      workflowDefaults(inputs, {
        text: "kept prompt",
        guidance: 3.5,
        extra_passthrough: "kept",
      }),
    ).toEqual({
      text: "kept prompt",
      steps: 8,
      guidance: 3.5,
      transparent_background: false,
      size: "1024x1024",
      extra_passthrough: "kept",
    });
  });

  it("parses and validates typed values", () => {
    expect(
      parseWorkflowInputs(
        JSON.stringify({
          text: "stone arch",
          steps: 12,
          guidance: 3.5,
          transparent_background: true,
          size: "512x512",
          extra_passthrough: "kept",
        }),
        inputs,
      ),
    ).toMatchObject({
      text: "stone arch",
      steps: 12,
      guidance: 3.5,
      transparent_background: true,
      size: "512x512",
      extra_passthrough: "kept",
    });
  });

  it("throws useful validation errors", () => {
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ steps: 12 }), inputs),
    ).toThrow("prompt is required.");
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ text: "x", steps: 12.5 }), inputs),
    ).toThrow("steps must be an integer.");
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ text: "x", guidance: 11 }), inputs),
    ).toThrow("guidance must be <= 10.");
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ text: "x", size: "128x128" }), inputs),
    ).toThrow("size must be one of: 512x512, 1024x1024.");
  });

  it("writes real route mappings including size and workflow extensions", () => {
    const body = workflowDefaults(mappedInputs, { model: "extension" }, mapping);
    expect(body).toEqual({
      model: "extension",
      size: "1024x768",
      extensions: { "com.vividfish.workflow": { steps: 8 } },
    });

    setWorkflowInputValue(body, mappedInputs, mappedInputs[1], 512, mapping);
    setWorkflowInputValue(body, mappedInputs, mappedInputs[3], 12, mapping);

    expect(body.size).toBe("512x768");
    expect(body.extensions).toEqual({
      "com.vividfish.workflow": { steps: 12 },
    });
    expect(readWorkflowInputValue(body, mappedInputs, mappedInputs[1], mapping)).toBe(512);
  });

  it("combines shared request_field size without explicit mapping", () => {
    const body = workflowDefaults(mappedInputs);
    expect(body.size).toBe("1024x768");

    setWorkflowInputValue(body, mappedInputs, mappedInputs[1], 640);
    expect(body.size).toBe("640x768");
  });

  it("keeps extension form fields as JSON strings for multipart requests", () => {
    const form = {
      prompt: "edit this",
      size: "1024x768",
      extensions:
        '{"com.example.other":{"kept":true},"com.vividfish.workflow":{"steps":8}}',
    };

    setWorkflowInputValue(form, mappedInputs, mappedInputs[3], 10, mapping);

    expect(form.extensions).toBe(
      '{"com.example.other":{"kept":true},"com.vividfish.workflow":{"steps":10}}',
    );
  });

  it("removes numeric fields when users clear them", () => {
    const body = { steps: 8 };
    setWorkflowInputValue(body, inputs, inputs[1], undefined);
    expect(body).toEqual({});
  });
});

describe("WorkflowInputs", () => {
  it("updates the exact native request field and preserves advanced JSON", () => {
    const onChange = vi.fn();
    render(
      <WorkflowInputs
        inputs={inputs}
        json={JSON.stringify({ text: "old", extra_passthrough: "kept" }, null, 2)}
        onChange={onChange}
      />,
    );

    const prompt = screen.getByRole("textbox", { name: "prompt" });
    fireEvent.change(prompt, { target: { value: "new" } });

    const last = JSON.parse(onChange.mock.calls.at(-1)?.[0] ?? "{}");
    expect(last).toMatchObject({
      text: "new",
      extra_passthrough: "kept",
    });
    expect(last.prompt).toBeUndefined();
  });

  it("renders typed controls and explains file and ignored inputs", () => {
    render(
      <WorkflowInputs
        inputs={inputs}
        json={JSON.stringify(workflowDefaults(inputs), null, 2)}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("spinbutton", { name: "steps" })).toHaveAttribute(
      "max",
      "50",
    );
    expect(screen.getByRole("checkbox", { name: "transparent_background" }))
      .toBeInTheDocument();
    expect(
      screen.getByText(
        "File value is handled by the existing workflow file picker.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Known input, but this route ignores it for the selected workflow.",
      ),
    ).toBeVisible();
  });

  it("disables guided controls while advanced JSON is malformed", () => {
    render(<WorkflowInputs inputs={inputs} json="{" onChange={() => undefined} />);

    expect(screen.getByText("Workflow inputs must be a JSON object.")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "prompt" })).toBeDisabled();
  });

  it("renders object values as advanced-only instead of stringifying them", () => {
    render(
      <WorkflowInputs
        inputs={[{ name: "metadata", type: "object" }]}
        json={JSON.stringify({ metadata: { keep: true } })}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("textbox", { name: "metadata" })).toHaveValue(
      '{\n  "keep": true\n}',
    );
    expect(screen.getByRole("textbox", { name: "metadata" })).toBeDisabled();
  });
});
