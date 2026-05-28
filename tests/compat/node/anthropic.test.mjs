// Anthropic Node SDK probe.
//
// Run only after `npm install` in this directory and after exporting
// OPENAI_PROXY_BASE_URL / OPENAI_PROXY_API_KEY. Live model calls are
// gated behind OPENAI_PROXY_SMOKE_LIVE_LLM=1.

import test from "node:test";
import assert from "node:assert/strict";

const baseURL = process.env.OPENAI_PROXY_BASE_URL?.replace(/\/$/, "");
const apiKey = process.env.OPENAI_PROXY_API_KEY;
const liveLLM = process.env.OPENAI_PROXY_SMOKE_LIVE_LLM === "1";
const model = process.env.OPENAI_PROXY_SMOKE_ANTHROPIC_MODEL || "default";
const timeout = Number(process.env.OPENAI_PROXY_SMOKE_TIMEOUT || "10") * 1000;

function requireApiKey(t) {
  if (!apiKey) {
    t.skip("OPENAI_PROXY_API_KEY not set; authenticated SDK probe disabled");
  }
}

function requireLiveLLM(t) {
  requireApiKey(t);
  if (!liveLLM) {
    t.skip("OPENAI_PROXY_SMOKE_LIVE_LLM not set; live LLM probe disabled");
  }
}

if (!baseURL) {
  test("anthropic-node probe disabled", { skip: "OPENAI_PROXY_BASE_URL not set" }, () => {});
} else {
  const { default: Anthropic } = await import("@anthropic-ai/sdk");
  const client = new Anthropic({
    baseURL: `${baseURL}/anthropic`,
    apiKey: apiKey || "anonymous",
    maxRetries: 0,
    timeout,
  });

  test("client exposes messages and models resources", () => {
    assert.equal(typeof client.messages.create, "function");
    assert.equal(typeof client.messages.stream, "function");
    assert.equal(typeof client.messages.countTokens, "function");
    assert.equal(typeof client.models.list, "function");
  });

  test("models.list returns a page-like object", async (t) => {
    requireApiKey(t);
    const models = await client.models.list({ limit: 20 });
    assert.ok(Array.isArray(models.data));
  });

  test("messages.countTokens returns input token count", async (t) => {
    requireApiKey(t);
    const result = await client.messages.countTokens({
      model,
      messages: [{ role: "user", content: "compatibility probe" }],
    });
    assert.equal(typeof result.input_tokens, "number");
    assert.ok(result.input_tokens > 0);
  });

  test("messages.create validates invalid max_tokens", async (t) => {
    requireApiKey(t);
    await assert.rejects(
      client.messages.create({
        model,
        max_tokens: 0,
        messages: [{ role: "user", content: "compatibility probe" }],
      }),
      (error) => error?.status === 400,
    );
  });

  test("messages.create returns text content", async (t) => {
    requireLiveLLM(t);
    const message = await client.messages.create({
      model,
      max_tokens: 8,
      messages: [{ role: "user", content: "compatibility probe" }],
    });
    assert.equal(message.type, "message");
    assert.equal(message.role, "assistant");
    assert.ok(Array.isArray(message.content));
    assert.ok(message.content.length > 0);
    assert.equal(message.content[0].type, "text");
    assert.equal(typeof message.content[0].text, "string");
    assert.ok(message.content[0].text.length > 0);
  });

  test("messages.create streaming emits Anthropic event names", async (t) => {
    requireLiveLLM(t);
    const stream = await client.messages.create({
      model,
      max_tokens: 8,
      stream: true,
      messages: [{ role: "user", content: "compatibility probe" }],
    });
    const observed = new Set();
    let text = "";
    for await (const event of stream) {
      observed.add(event.type);
      if (event.type === "content_block_delta" && event.delta?.type === "text_delta") {
        text += event.delta.text || "";
      }
    }
    const requiredEvents = [
      "message_start",
      "content_block_start",
      "content_block_delta",
      "message_stop",
    ];
    for (const eventName of requiredEvents) {
      assert.ok(
        observed.has(eventName),
        `missing ${eventName}; observed=${Array.from(observed).join(",")}`,
      );
    }
    assert.ok(text.length > 0);
  });
}
