// Vercel AI SDK Node probe.
//
// Run only after `npm install` in this directory and after exporting
// AZIZAM_BASE_URL / AZIZAM_API_KEY.
//
//   cd tests/compat/node && npm install
//   AZIZAM_BASE_URL=http://127.0.0.1:8080 \
//   AZIZAM_API_KEY=sk-... \
//   AZIZAM_SMOKE_LIVE_LLM=1 \
//   AZIZAM_SMOKE_LIVE_EMBEDDINGS=1 \
//   node --test vercel-ai-sdk.test.mjs

import test from "node:test";
import assert from "node:assert/strict";

const baseURL = process.env.AZIZAM_BASE_URL;
const apiKey = process.env.AZIZAM_API_KEY || "anonymous";
const liveLLM = process.env.AZIZAM_SMOKE_LIVE_LLM === "1";
const liveEmbeddings = process.env.AZIZAM_SMOKE_LIVE_EMBEDDINGS === "1";

if (!baseURL) {
  test("vercel-ai-sdk probe disabled", { skip: "AZIZAM_BASE_URL not set" }, () => {});
} else {
  const [{ generateText, embed }, { createOpenAI }] = await Promise.all([
    import("ai"),
    import("@ai-sdk/openai"),
  ]);

  const openai = createOpenAI({
    baseURL: baseURL.replace(/\/$/, "") + "/v1",
    apiKey,
  });

  test(
    "generateText returns text envelope",
    { skip: liveLLM ? false : "AZIZAM_SMOKE_LIVE_LLM=1 not set" },
    async () => {
      const result = await generateText({
        model: openai.chat(process.env.AZIZAM_SMOKE_OPENAI_MODEL || "default"),
        prompt: "Reply with the word ok.",
        maxOutputTokens: 8,
        maxRetries: 0,
        abortSignal: AbortSignal.timeout(10_000),
      });

      assert.equal(typeof result.text, "string");
      assert.ok(result.text.length > 0);
      assert.equal(typeof result.finishReason, "string");
      assert.equal(typeof result.usage, "object");
    },
  );

  test(
    "embed returns vector envelope",
    { skip: liveEmbeddings ? false : "AZIZAM_SMOKE_LIVE_EMBEDDINGS=1 not set" },
    async () => {
      const result = await embed({
        model: openai.embedding(
          process.env.AZIZAM_SMOKE_EMBEDDINGS_MODEL || "nomic-embed-text-v1.5",
        ),
        value: "compatibility probe",
        maxRetries: 0,
        abortSignal: AbortSignal.timeout(10_000),
      });

      assert.ok(Array.isArray(result.embedding));
      assert.ok(result.embedding.length > 0);
      assert.equal(typeof result.embedding[0], "number");
      assert.equal(result.value, "compatibility probe");
      assert.equal(typeof result.usage, "object");
    },
  );
}
