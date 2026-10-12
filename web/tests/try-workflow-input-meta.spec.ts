import { describe, expect, it } from "vitest";
import catalog from "./fixtures/workflows-ltx-i2v.json";
import {
  assetIdFromPasted,
  inputHint,
  missingMessage,
  inputLabel,
  inputPlaceholder,
  isPlausibleMediaReference,
  outputKindOf,
} from "@/app/try/workflow-input-meta";
import {
  isMediaBodyInput,
  workflowDefaults,
  workflowInputProblems,
  type WorkflowInput,
  type WorkflowInputMapping,
} from "@/app/try/workflow-inputs";

// The real catalog row the gateway builds for "LTX 2.5 Distilled I2V"
// (gateway.workflows_catalog.build_workflows_catalog, manifests/workflows/
// video-ltx-2.5-i2v.yml). The row's `type` for the picture is "string": the
// manifest's `asset` type does not survive into the row.
const workflow = catalog.workflows[0];
const inputs = workflow.inputs as WorkflowInput[];
const mapping = workflow.openapi.execution.async.fields as WorkflowInputMapping;
const image = inputs.find((input) => input.name === "image")!;
const prompt = inputs.find((input) => input.name === "prompt")!;

describe("media inputs in the request body", () => {
  it("finds the picture input from the real catalog row, not from its name", () => {
    expect(image.type).toBe("string");
    expect(isMediaBodyInput(image, mapping)).toBe(true);
    expect(isMediaBodyInput(prompt, mapping)).toBe(false);
    // The same input under another name is still media: the description the
    // gateway writes for every asset input is the signal.
    expect(
      isMediaBodyInput({ ...image, name: "first_frame" }, {
        first_frame: { in: "body", field: "input_reference" },
      }),
    ).toBe(true);
  });

  it("accepts an explicit asset flag and the asset type in the request body", () => {
    expect(
      isMediaBodyInput({ name: "ref", type: "string", asset: true }, {}),
    ).toBe(true);
    expect(
      isMediaBodyInput(
        { name: "ref", type: "asset", applies: "Request body" },
        {},
      ),
    ).toBe(true);
  });

  it("leaves multipart file uploads and ignored inputs to their own controls", () => {
    expect(
      isMediaBodyInput({ name: "ref", type: "asset", applies: "File upload" }, {}),
    ).toBe(false);
    expect(
      isMediaBodyInput({ ...image, ignored: true, accepted: false }, mapping),
    ).toBe(false);
  });

  it("knows which media kind to offer", () => {
    expect(outputKindOf(workflow.outputs)).toBe("video");
    expect(outputKindOf([{ kind: "audio" }])).toBe("audio");
    expect(outputKindOf([{ type: "image" }])).toBe("image");
    expect(outputKindOf(undefined)).toBe("other");
  });
});

describe("member-facing words for workflow inputs", () => {
  it("names the real inputs in plain words", () => {
    expect(inputLabel(image, "video")).toBe("Starting picture");
    expect(inputLabel(prompt, "video")).toBe("Describe the video");
    expect(inputLabel(prompt, "image")).toBe("Describe the picture");
    expect(
      inputLabel(inputs.find((input) => input.name === "negative_prompt")!, "video"),
    ).toBe("Avoid (optional)");
  });

  it("keeps an unknown input's own words rather than inventing a name", () => {
    expect(inputLabel({ name: "guidance", type: "number" }, "image")).toBe(
      "Guidance",
    );
    // Never the raw field name a developer typed.
    expect(inputLabel({ name: "batch_size", type: "integer" }, "image")).toBe(
      "Batch size",
    );
    expect(inputLabel(prompt, "other")).toBe("Prompt");
  });

  it("uses the manifest's label and help first", () => {
    const steps = {
      name: "steps",
      type: "integer",
      label: "number of steps",
      help: "More steps add detail but take longer.",
      description: "Turbo model; 8-12 steps is the community sweet spot.",
    };
    expect(inputLabel(steps, "image")).toBe("Number of steps");
    expect(inputHint(steps, "image")).toBe(
      "More steps add detail but take longer.",
    );
    expect(
      inputLabel({ name: "reference", type: "string", asset: true, label: "a face photo" }, "other"),
    ).toBe("Face photo");
  });

  it("asks for a missing picture by what it is", () => {
    expect(
      missingMessage(
        { name: "source_image", type: "string", asset: true, media_type: "image/*" },
        "image",
      ),
    ).toBe("Choose a picture first");
  });

  it("gives a helper line with no developer words", () => {
    for (const input of inputs) {
      const hint = inputHint(input, "video") ?? "";
      expect(hint).not.toMatch(
        /request body|writes|input_reference|data uri|string|json/i,
      );
    }
    expect(inputHint(image, "video")).toMatch(/picture/i);
  });

  it("shows an example in the placeholder, never the word value", () => {
    expect(inputPlaceholder(prompt, "video")).toMatch(/camera|drift|move|wave/i);
    for (const input of inputs) {
      expect(inputPlaceholder(input, "video") ?? "").not.toBe("value");
    }
  });
});

describe("a form that starts empty", () => {
  it("does not seed the request from the catalog's illustrative example", () => {
    const seeded = workflowDefaults(
      inputs,
      workflow.example_request.body as Record<string, unknown>,
      mapping,
    );
    // The example body carried: prompt "A small teal fish mark...", "value"
    // for negative_prompt and input_reference, seed 0, seconds 1, size
    // 1024x1024. None of that is the person's input.
    expect(seeded.prompt).toBeUndefined();
    expect(seeded.negative_prompt).toBeUndefined();
    expect(seeded.input_reference).toBeUndefined();
    expect(seeded.seed).toBeUndefined();
    expect(seeded.seconds).toBeUndefined();
    expect(seeded.size).toBeUndefined();
    expect(JSON.stringify(seeded)).not.toContain("value");
    // The route's own model and the manifest's real defaults stay.
    expect(seeded.model).toBe("video-ltx-2.5-i2v");
    expect(seeded.image_strength).toBe(0.7);
  });
});

describe("what Run checks before it sends anything", () => {
  const filled = {
    model: "video-ltx-2.5-i2v",
    prompt: "A paper boat drifts across a pond.",
    input_reference: "data:image/png;base64,iVBORw0KGgo=",
  };

  it("asks for the missing picture in plain words", () => {
    const problems = workflowInputProblems(
      JSON.stringify({ ...filled, input_reference: undefined }),
      inputs,
      mapping,
      "video",
    );
    expect(problems).toEqual({ image: "Choose a picture to start from" });
  });

  it("asks for the missing description", () => {
    const problems = workflowInputProblems(
      JSON.stringify({ ...filled, prompt: "  " }),
      inputs,
      mapping,
      "video",
    );
    expect(problems.prompt).toBe("Describe the video first");
    expect(problems.image).toBeUndefined();
  });

  it("refuses placeholder text where a picture belongs", () => {
    const problems = workflowInputProblems(
      JSON.stringify({ ...filled, input_reference: "value" }),
      inputs,
      mapping,
      "video",
    );
    expect(problems.image).toBe("Choose a picture to start from");
  });

  it("passes a complete request", () => {
    expect(
      workflowInputProblems(JSON.stringify(filled), inputs, mapping, "video"),
    ).toEqual({});
  });

  it("does not report a problem for an unreadable JSON box", () => {
    expect(workflowInputProblems("{", inputs, mapping, "video")).toEqual({});
  });
});

describe("media references", () => {
  it("recognises what the gateway accepts and rejects the rest", () => {
    const hex = "a".repeat(64);
    for (const ok of [
      "data:image/png;base64,iVBORw0KGgo=",
      "https://example.com/a.png",
      "http://192.168.1.2/a.png",
      `img_${hex}`,
      `asset:img_${hex}`,
      `vid_${hex}`,
    ])
      expect(isPlausibleMediaReference(ok), ok).toBe(true);
    for (const bad of ["value", "", "  ", "photo.png", "/etc/passwd", "ftp://x/y.png"])
      expect(isPlausibleMediaReference(bad), bad).toBe(false);
  });

  it("takes an asset id out of a pasted Library link", () => {
    const hex = "b".repeat(64);
    expect(assetIdFromPasted(`img_${hex}`)).toBe(`img_${hex}`);
    expect(
      assetIdFromPasted(`https://ai.vivid.fish/me/api/media/img_${hex}/download?scope=me`),
    ).toBe(`img_${hex}`);
    expect(
      assetIdFromPasted(`https://ai.vivid.fish/library/?asset=img_${hex}`),
    ).toBe(`img_${hex}`);
    expect(assetIdFromPasted("https://example.com/cat.png")).toBe(
      "https://example.com/cat.png",
    );
  });
});
