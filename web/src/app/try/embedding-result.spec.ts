import { describe, expect, it } from "vitest";
import { summarizeEmbedding } from "@/app/try/embedding-result";

describe("summarizeEmbedding", () => {
  it("summarizes two inputs with twelve dimensions and exact JSON", () => {
    const embedding = [0.123456789012345, -0.5, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
    const payload = { model: "example-embed", data: [
      { index: 0, embedding }, { index: 1, embedding: [99] },
    ], usage: { prompt_tokens: 12, total_tokens: 12 } };
    expect(summarizeEmbedding(payload)).toEqual({
      model: "example-embed", inputs: 2, dimensions: 12,
      preview: embedding.slice(0, 8), more: 4,
      json: "[0.123456789012345,-0.5,0,1,2,3,4,5,6,7,8,9]",
    });
    expect(embedding).toHaveLength(12);
  });

  it("previews all three numbers with no remainder", () => {
    expect(summarizeEmbedding({ model: "small", data: [{ index: 0, embedding: [1, 2, 3] }] })).toEqual({
      model: "small", inputs: 1, dimensions: 3, preview: [1, 2, 3], more: 0, json: "[1,2,3]",
    });
  });

  it("uses null for a missing or non-string model without guessing", () => {
    for (const model of [undefined, null, 12, {}, "", "   "]) {
      expect(summarizeEmbedding({ model, data: [{ embedding: [0] }] })?.model).toBeNull();
    }
    expect(summarizeEmbedding({ data: [{ embedding: [0] }] })?.model).toBeNull();
  });

  it("rejects missing, empty or non-array data", () => {
    for (const payload of [{}, { data: [] }, { data: {} }, { data: null }]) {
      expect(summarizeEmbedding(payload)).toBeNull();
    }
  });

  it("rejects a missing, empty or non-array first embedding", () => {
    for (const first of [null, {}, { embedding: [] }, { embedding: "1,2" }, { embedding: {} }]) {
      expect(summarizeEmbedding({ data: [first, { embedding: [1] }] })).toBeNull();
    }
  });

  it("rejects NaN, infinity, non-numbers and sparse embeddings", () => {
    for (const embedding of [[1, NaN], [Infinity], [-Infinity], ["1"], [null], new Array(2)]) {
      expect(summarizeEmbedding({ data: [{ embedding }] })).toBeNull();
    }
  });

  it("rejects non-object payloads", () => {
    for (const payload of [null, undefined, "answer", 12, false, []]) {
      expect(summarizeEmbedding(payload)).toBeNull();
    }
  });

  it("returns a detached preview without mutating the input", () => {
    const embedding = Object.freeze([0.5, -2]);
    const summary = summarizeEmbedding({ data: [{ embedding }] });
    expect(summary?.preview).not.toBe(embedding);
    summary?.preview.push(3);
    expect(embedding).toEqual([0.5, -2]);
    expect(summary?.json).toBe("[0.5,-2]");
  });
});
