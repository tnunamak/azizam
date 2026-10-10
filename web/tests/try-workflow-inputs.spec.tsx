import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

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
    description: "Sampling steps.",
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

const assetBodyInputs: WorkflowInput[] = [
  {
    name: "reference_image",
    type: "asset",
    required: true,
    applies: "Request body",
  },
];

const rawSizeInputs: WorkflowInput[] = [
  { name: "size", type: "string", request_field: "size", default: "1280x720" },
  { name: "width", type: "integer", default: 1024, request_field: "size" },
  { name: "height", type: "integer", default: 768, request_field: "size" },
];

const unsupportedCombineInputs: WorkflowInput[] = [
  { name: "left", type: "integer", default: 1, request_field: "crop" },
  { name: "top", type: "integer", default: 2, request_field: "crop" },
];

afterEach(cleanup);

describe("workflow input helpers", () => {
  it("drops mapped upload examples from the form and extension", () => {
    const upload: WorkflowInput[] = [{ name: "picture", type: "string", asset: true }];
    const mapping: WorkflowInputMapping = {
      picture: { in: "file+extension", field: "image" },
    };
    const example = {
      model: "edit",
      image: "example.png",
      extensions: '{"other":{"keep":true},"com.azizam.workflow":{"image":"example.png"}}',
    };
    const defaults = workflowDefaults(upload, example, mapping);
    expect(defaults).not.toHaveProperty("image");
    expect(JSON.parse(defaults.extensions as string)).toEqual({
      other: { keep: true },
      "com.azizam.workflow": {},
    });
    expect(defaults.model).toBe("edit");
    expect(example.image).toBe("example.png");
  });

  it("clears editable examples before applying defaults inside request envelopes", () => {
    const inputs: WorkflowInput[] = [
      { name: "seed", type: "integer", default: 42 },
      { name: "mode", type: "string", enum: ["fast", "best"] },
      { name: "format", type: "string", enum: ["png"] },
      { name: "steps", type: "integer", default: 8 },
    ];
    expect(workflowDefaults(inputs, {
      request: {
        model: "fixed-model",
        seed: 0,
        mode: "fast",
        format: "png",
        extensions: { "com.azizam.workflow": { steps: 1 }, other: { keep: true } },
      },
    }, { steps: { in: "extension", field: "steps" } })).toEqual({
      request: {
        model: "fixed-model",
        seed: 42,
        format: "png",
        extensions: { "com.azizam.workflow": { steps: 8 }, other: { keep: true } },
      },
    });
  });

  it("uses declared defaults while preserving extra protocol fields", () => {
    expect(
      workflowDefaults(inputs, {
        text: "kept prompt",
        guidance: 3.5,
        extra_passthrough: "kept",
      }),
    ).toEqual({
      text: "stone arch",
      steps: 8,
      transparent_background: false,
      size: "1024x1024",
      extra_passthrough: "kept",
    });
  });

  it("drops example values the manifest does not declare as a default", () => {
    // guidance has no default: 3.5 was the catalog's illustration, not a choice.
    expect(
      workflowDefaults(inputs, { guidance: 3.5, extra_passthrough: "kept" }),
    ).not.toHaveProperty("guidance");
  });

  it("replaces example combined size with declared defaults", () => {
    expect(workflowDefaults(mappedInputs, { size: "640x480" })).toEqual({
      size: "1024x768",
      extensions: { "com.azizam.workflow": { steps: 8 } },
    });
  });

  it("deep clones nested example data before adding defaults", () => {
    const example = {
      request: {
        model: "slow",
        extensions: { "com.azizam.workflow": { steps: 4 } },
      },
    };

    const defaults = workflowDefaults(mappedInputs, example, mapping);
    setWorkflowInputValue(defaults, mappedInputs, mappedInputs[3], 10, mapping);

    expect(defaults.request).toEqual({
      model: "slow",
      size: "1024x768",
      extensions: { "com.azizam.workflow": { steps: 10 } },
    });
    expect(example.request.extensions["com.azizam.workflow"].steps).toBe(4);
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
    ).toThrow("Prompt is required.");
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ text: "x", steps: 12.5 }), inputs),
    ).toThrow("Steps must be a whole number.");
    expect(() =>
      parseWorkflowInputs(JSON.stringify({ text: "x", guidance: 11 }), inputs),
    ).toThrow("Guidance must be at most 10.");
    expect(() =>
      parseWorkflowInputs(
        JSON.stringify({ text: "x", size: "128x128" }),
        inputs,
      ),
    ).toThrow("Size must be one of: 512x512, 1024x1024.");
  });

  it("writes real route mappings including size and workflow extensions", () => {
    const body = workflowDefaults(
      mappedInputs,
      { model: "extension" },
      mapping,
    );
    expect(body).toEqual({
      model: "extension",
      size: "1024x768",
      extensions: { "com.azizam.workflow": { steps: 8 } },
    });

    setWorkflowInputValue(body, mappedInputs, mappedInputs[1], 512, mapping);
    setWorkflowInputValue(body, mappedInputs, mappedInputs[3], 12, mapping);

    expect(body.size).toBe("512x768");
    expect(body.extensions).toEqual({
      "com.azizam.workflow": { steps: 12 },
    });
    expect(
      readWorkflowInputValue(body, mappedInputs, mappedInputs[1], mapping),
    ).toBe(512);
  });

  it("combines shared request_field size without explicit mapping", () => {
    const body = workflowDefaults(mappedInputs);
    expect(body.size).toBe("1024x768");

    setWorkflowInputValue(body, mappedInputs, mappedInputs[1], 640);
    expect(body.size).toBe("640x768");
  });

  it("leaves unsupported shared request fields to Advanced JSON", () => {
    const body = workflowDefaults(unsupportedCombineInputs);

    expect(body).toEqual({});
    setWorkflowInputValue(
      body,
      unsupportedCombineInputs,
      unsupportedCombineInputs[0],
      3,
    );
    expect(body).toEqual({});
  });

  it("keeps extension form fields as JSON strings for multipart requests", () => {
    const form = {
      prompt: "edit this",
      size: "1024x768",
      extensions:
        '{"com.example.other":{"kept":true},"com.azizam.workflow":{"steps":8}}',
    };

    setWorkflowInputValue(form, mappedInputs, mappedInputs[3], 10, mapping);

    expect(form.extensions).toBe(
      '{"com.example.other":{"kept":true},"com.azizam.workflow":{"steps":10}}',
    );
  });

  it("rejects invalid extension JSON instead of overwriting it", () => {
    expect(() =>
      parseWorkflowInputs(
        JSON.stringify({ extensions: '{"com.azizam.workflow":' }),
        mappedInputs,
        mapping,
      ),
    ).toThrow("extensions must be valid JSON before using guided controls.");
  });

  it("removes numeric fields when users clear them", () => {
    const body = { steps: 8 };
    setWorkflowInputValue(body, inputs, inputs[1], undefined);
    expect(body).toEqual({});
  });

  it("validates and edits required asset strings in JSON requests", () => {
    expect(() => parseWorkflowInputs("{}", assetBodyInputs)).toThrow(
      "Reference image is required.",
    );

    const body = {};
    setWorkflowInputValue(
      body,
      assetBodyInputs,
      assetBodyInputs[0],
      "https://example.test/reference.png",
    );

    expect(body).toEqual({
      reference_image: "https://example.test/reference.png",
    });
    expect(() =>
      parseWorkflowInputs(JSON.stringify(body), assetBodyInputs),
    ).not.toThrow();
  });

  it("lets literal size edit the raw size field beside width and height", () => {
    const body = workflowDefaults(rawSizeInputs);

    expect(body).toEqual({ size: "1280x720" });
    expect(readWorkflowInputValue(body, rawSizeInputs, rawSizeInputs[0])).toBe(
      "1280x720",
    );

    setWorkflowInputValue(body, rawSizeInputs, rawSizeInputs[0], "640x480");
    expect(body.size).toBe("640x480");

    setWorkflowInputValue(body, rawSizeInputs, rawSizeInputs[1], 320);
    expect(body.size).toBe("320x480");
  });
});

describe("WorkflowInputs", () => {
  it("keeps short-string Enter submission and long-string multiline editing", async () => {
    const submitted = vi.fn();
    function Form() {
      const [json, setJson] = useState(JSON.stringify({ prompt: "short" }));
      return (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submitted();
          }}
        >
          <WorkflowInputs
            inputs={[{ name: "prompt", type: "string" }]}
            json={json}
            onChange={setJson}
          />
          <button type="submit">Run</button>
        </form>
      );
    }
    const user = userEvent.setup();
    render(<Form />);
    const prompt = screen.getByRole("textbox", { name: "Prompt" });
    await user.click(prompt);
    await user.keyboard("{Enter}");
    expect(submitted).toHaveBeenCalledTimes(1);
    expect(prompt).toHaveValue("short");
    fireEvent.keyDown(prompt, { key: "Enter", isComposing: true });
    expect(submitted).toHaveBeenCalledTimes(1);
    await user.clear(prompt);
    await user.paste("x".repeat(81));
    await user.keyboard("{Enter}");
    expect(prompt).toHaveValue("x".repeat(81) + "\n");
    expect(submitted).toHaveBeenCalledTimes(1);
    // A short pasted multi-line prompt keeps its newline, grows instead of
    // hiding the second line, and Enter adds a line rather than submitting.
    await user.clear(prompt);
    await user.paste("a cat\non a mat");
    expect(prompt).toHaveValue("a cat\non a mat");
    expect(prompt.className).not.toContain("h-11");
    await user.keyboard("{Enter}");
    expect(submitted).toHaveBeenCalledTimes(1);
    await user.clear(prompt);
    screen.getByRole("button", { name: "Run" }).setAttribute("disabled", "");
    await user.keyboard("{Enter}");
    expect(submitted).toHaveBeenCalledTimes(1);
  });

  it("preserves the focused string control and caret across 80 characters", async () => {
    function ControlledInputs() {
      const [json, setJson] = useState(
        JSON.stringify({ prompt: "x".repeat(79) }),
      );
      return (
        <WorkflowInputs
          inputs={[{ name: "prompt", type: "string" }]}
          json={json}
          onChange={setJson}
        />
      );
    }
    const user = userEvent.setup();
    render(<ControlledInputs />);
    const prompt = screen.getByRole("textbox", { name: "Prompt" });
    await user.click(prompt);
    await user.keyboard("ab");
    expect(screen.getByRole("textbox", { name: "Prompt" })).toBe(prompt);
    expect(prompt).toHaveFocus();
    expect(prompt).toHaveValue("x".repeat(79) + "ab");
    expect((prompt as HTMLTextAreaElement).selectionStart).toBe(81);
    await user.keyboard("{Backspace}{Backspace}");
    expect(screen.getByRole("textbox", { name: "Prompt" })).toBe(prompt);
    expect(prompt).toHaveFocus();
    expect((prompt as HTMLTextAreaElement).selectionStart).toBe(79);
    await user.paste("long pasted text");
    expect(screen.getByRole("textbox", { name: "Prompt" })).toBe(prompt);
    expect(prompt).toHaveFocus();
    expect(prompt).toHaveValue("x".repeat(79) + "long pasted text");
    expect((prompt as HTMLTextAreaElement).selectionStart).toBe(95);
  });

  it("updates the exact native request field and preserves advanced JSON", () => {
    const onChange = vi.fn();
    render(
      <WorkflowInputs
        inputs={inputs}
        json={JSON.stringify(
          { text: "old", extra_passthrough: "kept" },
          null,
          2,
        )}
        onChange={onChange}
      />,
    );

    const prompt = screen.getByRole("textbox", { name: /Prompt Required/ });
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
        onFileChange={() => undefined}
        technical
      />,
    );

    expect(screen.getByRole("spinbutton", { name: "Steps" })).toHaveAttribute(
      "max",
      "50",
    );
    expect(screen.getByText("Sampling steps.")).toBeVisible();
    expect(
      screen.getByRole("checkbox", { name: "Transparent background" }),
    ).toBeInTheDocument();
    // A file input gets the picture picker itself, not a text box.
    const picture = screen.getByRole("group", { name: /Source image/ });
    expect(
      within(picture).getByRole("button", { name: "Choose a file" }),
    ).toBeVisible();
    expect(screen.queryByRole("textbox", { name: /Source image/ })).toBeNull();
    expect(
      document.querySelector('[data-technical-input="source_image"]'),
    ).toHaveTextContent("Sent as the file part source_image.");
    // An ignored input is not a control; it is explained under Input details.
    expect(screen.queryByRole("textbox", { name: "Seed" })).toBeNull();
    expect(screen.queryByRole("spinbutton", { name: "Seed" })).toBeNull();
    const technical = document.querySelector('[data-technical-input="seed"]')!;
    expect(technical).toHaveTextContent(
      "Known input, but this route ignores it for the selected workflow.",
    );
  });

  it("renders mapped body asset inputs as a picker with the link under Advanced", () => {
    const onChange = vi.fn();
    render(
      <WorkflowInputs
        inputs={assetBodyInputs}
        json={JSON.stringify({ reference_image: "" })}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("button", { name: "Choose a file" })).toBeVisible();
    const link = screen.getByRole("textbox", {
      name: "Link, Library link or data URI",
    });
    fireEvent.change(link, {
      target: { value: "https://example.test/reference.png" },
    });
    expect(JSON.parse(onChange.mock.calls.at(-1)?.[0] ?? "{}")).toEqual({
      reference_image: "https://example.test/reference.png",
    });
  });

  it("disables guided controls while advanced JSON is malformed", () => {
    render(
      <WorkflowInputs inputs={inputs} json="{" onChange={() => undefined} />,
    );

    expect(
      screen.getByText("Workflow inputs must be a JSON object."),
    ).toBeVisible();
    expect(
      screen.getByRole("textbox", { name: /Prompt Required/ }),
    ).toBeDisabled();
  });

  it("disables guided controls while extension JSON is malformed", () => {
    render(
      <WorkflowInputs
        inputs={mappedInputs}
        mapping={mapping}
        json={JSON.stringify({ extensions: '{"com.azizam.workflow":' })}
        onChange={() => undefined}
      />,
    );

    expect(
      screen.getByText(
        "extensions must be valid JSON before using guided controls.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("spinbutton", { name: "Steps" })).toBeDisabled();
  });

  it("explains unsupported combined controls instead of editing them", () => {
    render(
      <WorkflowInputs
        inputs={unsupportedCombineInputs}
        json={JSON.stringify({ crop: "1,2" })}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("spinbutton", { name: "Left" })).toBeDisabled();
    expect(
      screen.getAllByText(
        "This setting cannot be changed here yet.",
      ),
    ).toHaveLength(2);
  });

  it("renders object values as advanced-only instead of stringifying them", () => {
    render(
      <WorkflowInputs
        inputs={[{ name: "metadata", type: "object" }]}
        json={JSON.stringify({ metadata: { keep: true } })}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Metadata" })).toHaveValue(
      '{\n  "keep": true\n}',
    );
    expect(screen.getByRole("textbox", { name: "Metadata" })).toBeDisabled();
  });
});

it("allows required unsupported compound fields through Advanced JSON", () => {
  const inputs: WorkflowInput[] = [
    { name: "left", type: "integer", request_field: "crop", required: true },
    { name: "top", type: "integer", request_field: "crop", required: true },
  ];
  expect(parseWorkflowInputs('{"crop":"1,2","extra":true}', inputs)).toEqual({
    crop: "1,2",
    extra: true,
  });
});

describe("TM-12 optional inputs can return to unset", () => {
  const optional: WorkflowInput[] = [
    { name: "transparent_background", type: "boolean" },
    { name: "size", type: "string", enum: ["512x512", "1024x1024"] },
    { name: "mode", type: "string", enum: ["fast", "best"], required: true },
  ];

  function Harness({ onChange }: { onChange: (json: string) => void }) {
    const [json, setJson] = useState(
      JSON.stringify({ transparent_background: false, size: "1024x1024", mode: "fast" }),
    );
    return (
      <WorkflowInputs
        inputs={optional}
        json={json}
        onChange={(next) => {
          setJson(next);
          onChange(next);
        }}
      />
    );
  }

  it("omits a cleared optional boolean and says it is not set", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    expect(screen.getByText("Disabled")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Clear Transparent background" }));
    const last = JSON.parse(onChange.mock.calls.at(-1)?.[0] ?? "{}");
    expect(last).not.toHaveProperty("transparent_background");
    expect(screen.getByText("Not set")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Clear Transparent background" })).toBeNull();
  });

  it("omits an optional enum set to Not set; a required enum has no Not set", async () => {
    Element.prototype.scrollIntoView ??= () => undefined;
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const size = screen.getByRole("combobox", { name: "Size" });
    size.focus();
    fireEvent.keyDown(size, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Not set" }));
    const last = JSON.parse(onChange.mock.calls.at(-1)?.[0] ?? "{}");
    expect(last).not.toHaveProperty("size");
    expect(last.mode).toBe("fast");
    const mode = screen.getByRole("combobox", { name: /Mode Required/ });
    mode.focus();
    fireEvent.keyDown(mode, { key: "Enter" });
    expect(await screen.findByRole("option", { name: "best" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Not set" })).toBeNull();
  });
});
