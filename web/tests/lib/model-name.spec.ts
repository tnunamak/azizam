import { describe, expect, test } from "vitest";
import {
  isPathLikeModelId,
  modelDisplayName,
  safeModelId,
  scrubModelPaths,
  technicalModelDetails,
} from "@/lib/model-name";

const GGUF = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf";

describe("model names never expose a path on the gateway host", () => {
  test("technical model details preserve owner IDs without mutating member scrubbing", () => {
    const payload = { requested_model: GGUF, resolved_model: GGUF, fallback_model: GGUF, metadata: { resolves_to: { chat: GGUF } } };
    const member = technicalModelDetails(payload, false);
    expect(JSON.stringify(member)).not.toContain("/media/");
    expect(member.fallback_model).toBe(safeModelId(GGUF));
    expect(technicalModelDetails(payload, true)).toBe(payload);
    expect(payload.resolved_model).toBe(GGUF);
    expect(technicalModelDetails(payload, true).metadata.resolves_to.chat).toBe(GGUF);
  });
  test.each([
    GGUF,
    "C:\\Models\\llama-3.gguf",
    "~/models/flux.safetensors",
    "./local/model.bin",
    "model.gguf",
  ])("%s is path-like", (id) => {
    expect(isPathLikeModelId(id)).toBe(true);
    expect(safeModelId(id)).not.toMatch(/[\\/]/);
  });

  test.each(["default", "decide-quality", "openai/gpt-4o", "Qwen/Qwen3-8B", "tts-1"])(
    "%s is a normal id and stays as written",
    (id) => {
      expect(isPathLikeModelId(id)).toBe(false);
      expect(safeModelId(id)).toBe(id);
    },
  );

  test("safeModelId keeps the short file name", () => {
    expect(safeModelId(GGUF)).toBe("Qwen3.8-27B-Uncensored-Q4_K_M.gguf");
  });

  test("the display name reads as words, without the file's packing tag", () => {
    expect(modelDisplayName(GGUF)).toBe("Qwen3.8 27B Uncensored");
    expect(modelDisplayName("Qwen3.8-27B-Uncensored-Q4_K_M.gguf")).toBe(
      "Qwen3.8 27B Uncensored",
    );
    expect(modelDisplayName("/m/x/Llama-3-00001-of-00003.gguf")).toBe("Llama 3");
    expect(modelDisplayName("gemma-3-12b-it-IQ4_XS.gguf")).toBe("gemma 3 12b it");
    expect(modelDisplayName("flux2_klein_4b_fp8_scaled.safetensors")).toBe(
      "flux2 klein 4b",
    );
  });

  test("a capability's recommended name that is a file stem is cleaned too", () => {
    const name = modelDisplayName("default", undefined, [
      { use_name: "default", recommended_model: "Qwen3.8-27B-Uncensored-Q4_K_M" },
    ]);
    expect(name).toBe("Qwen3.8 27B Uncensored");
    expect(
      modelDisplayName("fast", undefined, [
        { use_name: "fast", recommended_model: "Qwen 3.5 8B" },
      ]),
    ).toBe("Qwen 3.5 8B");
  });

  test("a catalog title wins, and a path-shaped title is still not shown", () => {
    expect(
      modelDisplayName(GGUF, { models: [{ id: GGUF, title: "Qwen Chat" }] }),
    ).toBe("Qwen Chat");
    expect(
      modelDisplayName("a", { models: [{ id: "a", title: "/x/Some-File.gguf" }] }),
    ).toBe("Some File");
  });

  test("scrubModelPaths shortens model fields and leaves URL paths alone", () => {
    const payload = {
      models: [
        {
          id: GGUF,
          display_name: GGUF,
          use: { model: GGUF, path: "/v1/chat/completions" },
          resolves_to: { chat: GGUF },
          examples: [{ body: { model: GGUF, messages: ["/etc/not-a-model"] } }],
        },
      ],
    };
    const scrubbed = scrubModelPaths(payload);
    const file = "Qwen3.8-27B-Uncensored-Q4_K_M.gguf";
    expect(scrubbed.models[0].id).toBe(file);
    expect(scrubbed.models[0].display_name).toBe(file);
    expect(scrubbed.models[0].use).toEqual({ model: file, path: "/v1/chat/completions" });
    expect(scrubbed.models[0].resolves_to).toEqual({ chat: file });
    expect(scrubbed.models[0].examples[0].body.model).toBe(file);
    expect(scrubbed.models[0].examples[0].body.messages).toEqual(["/etc/not-a-model"]);
    expect(JSON.stringify(scrubbed)).not.toContain("/media/");
  });
});

test("scrubModelPaths shortens a declared fallback model path for members", () => {
  const scrubbed = scrubModelPaths({ declared_fallback: "/private/models/Fallback.gguf" });
  expect(JSON.stringify(scrubbed)).not.toContain("/private/models/");
});

test("a grouped capability's recommendation does not name other workflows", () => {
  const music = [{ use_name: "audio.generate.ace-step", recommended_model: "ACE-Step 1.5 XL Turbo", use_names: ["audio.generate.ace-step", "audio.generate.yue2-song"] }];
  expect(modelDisplayName("audio.generate.yue2-song", undefined, music)).not.toContain("ACE-Step");
  expect(modelDisplayName("audio.generate.ace-step", undefined, music)).toBe("ACE-Step 1.5 XL Turbo");
});
