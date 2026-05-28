// OpenAI Node SDK probe.
//
// Run only after `npm install` in this directory and after exporting
// OPENAI_PROXY_BASE_URL / OPENAI_PROXY_API_KEY. Live chat/Responses calls
// are gated behind OPENAI_PROXY_SMOKE_LIVE_LLM=1.
//
//   cd tests/compat/node && npm install
//   OPENAI_PROXY_BASE_URL=http://127.0.0.1:8080 \
//   OPENAI_PROXY_API_KEY=sk-... \
//   OPENAI_PROXY_SMOKE_LIVE_LLM=1 \
//   node --test openai.test.mjs

import test from "node:test";
import assert from "node:assert/strict";

const baseURL = process.env.OPENAI_PROXY_BASE_URL;
const apiKey = process.env.OPENAI_PROXY_API_KEY;
const liveLLM = process.env.OPENAI_PROXY_SMOKE_LIVE_LLM === "1";
const model = process.env.OPENAI_PROXY_SMOKE_OPENAI_MODEL || "default";

if (!baseURL) {
  test("openai-node probe disabled", { skip: "OPENAI_PROXY_BASE_URL not set" }, () => {});
} else {
  const { default: OpenAI } = await import("openai");
  const client = new OpenAI({
    baseURL: baseURL.replace(/\/$/, "") + "/v1",
    apiKey: apiKey || "anonymous",
  });

  test("GET /v1/models returns a list", async () => {
    const result = await client.models.list();
    assert.ok(Array.isArray(result.data));
  });

  test("POST /v1/embeddings returns vectors", async () => {
    const result = await client.embeddings.create({
      model: process.env.OPENAI_PROXY_SMOKE_EMBEDDINGS_MODEL || "nomic-embed-text-v1.5",
      input: "compatibility probe",
    });
    assert.equal(result.object, "list");
    assert.ok(Array.isArray(result.data));
    assert.ok(result.data.length > 0);
    assert.ok(Array.isArray(result.data[0].embedding));
    assert.ok(result.data[0].embedding.length > 0);
    assert.equal(typeof result.data[0].embedding[0], "number");
  });

  test(
    "POST /v1/chat/completions returns visible content",
    { skip: liveLLM ? false : "OPENAI_PROXY_SMOKE_LIVE_LLM=1 not set" },
    async () => {
      const result = await client.chat.completions.create({
        model,
        messages: [{ role: "user", content: "compatibility probe" }],
        max_tokens: 8,
      });
      const content = result.choices?.[0]?.message?.content;
      assert.equal(typeof content, "string");
      assert.ok(content.length > 0);
    },
  );

  test(
    "POST /v1/chat/completions streams content deltas",
    { skip: liveLLM ? false : "OPENAI_PROXY_SMOKE_LIVE_LLM=1 not set" },
    async () => {
      const stream = await client.chat.completions.create({
        model,
        stream: true,
        messages: [{ role: "user", content: "compatibility probe" }],
        max_tokens: 8,
      });
      let text = "";
      for await (const chunk of stream) {
        text += chunk.choices?.[0]?.delta?.content || "";
      }
      assert.ok(text.length > 0);
    },
  );

  test(
    "POST /v1/responses returns output text",
    { skip: liveLLM ? false : "OPENAI_PROXY_SMOKE_LIVE_LLM=1 not set" },
    async () => {
      const result = await client.responses.create({
        model,
        input: "compatibility probe",
        max_output_tokens: 8,
      });
      assert.equal(typeof result.output_text, "string");
      assert.ok(result.output_text.length > 0);
    },
  );
}
