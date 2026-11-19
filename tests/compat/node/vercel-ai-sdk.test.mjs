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
  const [{ generateText, streamText, embed, tool, jsonSchema, Output }, { createOpenAI }] = await Promise.all([
    import("ai"),
    import("@ai-sdk/openai"),
  ]);

  const openai = createOpenAI({
    baseURL: baseURL.replace(/\/$/, "") + "/v1",
    apiKey,
  });

  const chatModel = () => openai.chat(process.env.AZIZAM_SMOKE_OPENAI_MODEL || "default");
  const live = { skip: liveLLM ? false : "AZIZAM_SMOKE_LIVE_LLM=1 not set" };

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

  test("chat.streaming: streamText returns text and a finish reason", live, async () => {
    const result = streamText({
      model: chatModel(),
      prompt: "Reply with the word ok.",
      maxOutputTokens: 8,
      maxRetries: 0,
      abortSignal: AbortSignal.timeout(30_000),
    });
    let text = "";
    for await (const part of result.textStream) text += part;
    assert.ok(text.length > 0);
    assert.equal(typeof (await result.finishReason), "string");
  });

  test("chat.tools: a forced tool call returns get_weather", live, async () => {
    const result = await generateText({
      model: chatModel(),
      prompt: "What is the weather in Paris?",
      tools: {
        get_weather: tool({
          description: "Get the current weather for a city.",
          inputSchema: jsonSchema({
            type: "object",
            properties: { city: { type: "string" } },
            required: ["city"],
            additionalProperties: false,
          }),
        }),
      },
      toolChoice: "required",
      maxRetries: 0,
      abortSignal: AbortSignal.timeout(30_000),
    });
    assert.ok(result.toolCalls.length > 0);
    assert.equal(result.toolCalls[0].toolName, "get_weather");
    assert.equal(typeof result.toolCalls[0].input.city, "string");
    assert.ok(result.toolCalls[0].input.city.length > 0);
  });

  test("chat.structured_output: generateText returns a structured answer", live, async () => {
    const result = await generateText({
      model: chatModel(),
      prompt: "What is the capital of France? Answer in one word.",
      output: Output.object({
        schema: jsonSchema({
          type: "object",
          properties: { answer: { type: "string" } },
          required: ["answer"],
          additionalProperties: false,
        }),
      }),
      maxRetries: 0,
      abortSignal: AbortSignal.timeout(30_000),
    });
    assert.equal(typeof result.output.answer, "string");
    assert.ok(result.output.answer.length > 0);
  });

  test("responses.non_streaming: generateText returns Responses API text", live, async () => {
    const result = await generateText({
      model: openai.responses(process.env.AZIZAM_SMOKE_OPENAI_MODEL || "default"),
      prompt: "Reply with the word ok.",
      maxRetries: 0,
      abortSignal: AbortSignal.timeout(30_000),
    });
    assert.equal(typeof result.text, "string");
    assert.ok(result.text.length > 0);
  });

  test("anthropic.messages_streaming: needs the @ai-sdk/anthropic package", {
    skip: "not covered here: needs the @ai-sdk/anthropic package; anthropic.test.mjs covers Messages",
  }, () => {});

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
