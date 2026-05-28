// Ollama JavaScript SDK probe.
//
// Run only after `npm install` in this directory and after exporting
// OPENAI_PROXY_BASE_URL / OPENAI_PROXY_API_KEY. Live LLM and embedding
// round-trips are gated behind explicit smoke flags.

import test from "node:test";
import assert from "node:assert/strict";

const baseURL = process.env.OPENAI_PROXY_BASE_URL?.replace(/\/$/, "");
const apiKey = process.env.OPENAI_PROXY_API_KEY;
const liveLLM = process.env.OPENAI_PROXY_SMOKE_LIVE_LLM === "1";
const liveEmbeddings = process.env.OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS === "1";
const model = process.env.OPENAI_PROXY_SMOKE_OLLAMA_MODEL || "default";
const embeddingModel =
  process.env.OPENAI_PROXY_SMOKE_OLLAMA_EMBED_MODEL || "nomic-embed-text-v1.5";

function field(value, name) {
  if (value && typeof value === "object" && name in value) {
    return value[name];
  }
  return undefined;
}

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

function requireLiveEmbeddings(t) {
  requireApiKey(t);
  if (!liveEmbeddings) {
    t.skip("OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS not set; live embeddings probe disabled");
  }
}

if (!baseURL) {
  test("ollama-js probe disabled", { skip: "OPENAI_PROXY_BASE_URL not set" }, () => {});
} else {
  const { Ollama } = await import("ollama");
  const client = new Ollama({
    host: baseURL,
    headers: { Authorization: `Bearer ${apiKey || "anonymous"}` },
  });

  test("client exposes Ollama resources", () => {
    assert.equal(typeof client.list, "function");
    assert.equal(typeof client.chat, "function");
    assert.equal(typeof client.generate, "function");
    assert.equal(typeof client.embed, "function");
  });

  test("list returns model array", async (t) => {
    requireApiKey(t);
    const listing = await client.list();
    assert.ok(Array.isArray(field(listing, "models")));
  });

  test("ps returns running-model array", async (t) => {
    requireApiKey(t);
    const listing = await client.ps();
    assert.ok(Array.isArray(field(listing, "models")));
  });

  test("show returns model metadata", async (t) => {
    requireApiKey(t);
    const shown = await client.show({ model });
    assert.ok(field(shown, "details"));
  });

  test("version returns an Ollama-shaped version object", async (t) => {
    requireApiKey(t);
    const result = await client.version();
    assert.equal(typeof field(result, "version"), "string");
  });

  test("embed returns vector arrays", async (t) => {
    requireLiveEmbeddings(t);
    const result = await client.embed({
      model: embeddingModel,
      input: "compatibility probe",
    });
    const embeddings = field(result, "embeddings");
    assert.ok(Array.isArray(embeddings));
    assert.ok(Array.isArray(embeddings[0]));
    assert.equal(typeof embeddings[0][0], "number");
  });

  test("legacy embeddings returns a vector", async (t) => {
    requireLiveEmbeddings(t);
    const result = await client.embeddings({
      model: embeddingModel,
      prompt: "compatibility probe",
    });
    const embedding = field(result, "embedding");
    assert.ok(Array.isArray(embedding));
    assert.equal(typeof embedding[0], "number");
  });

  test("chat returns message content", async (t) => {
    requireLiveLLM(t);
    const result = await client.chat({
      model,
      stream: false,
      messages: [{ role: "user", content: "compatibility probe" }],
      options: { num_predict: 8 },
    });
    const message = field(result, "message");
    assert.equal(typeof field(message, "content"), "string");
  });

  test("generate returns text", async (t) => {
    requireLiveLLM(t);
    const result = await client.generate({
      model,
      stream: false,
      prompt: "compatibility probe",
      options: { num_predict: 8 },
    });
    assert.equal(typeof field(result, "response"), "string");
  });

  test("pull returns unsupported-operation error", async (t) => {
    requireApiKey(t);
    await assert.rejects(
      client.pull({ model: "llama3", stream: false }),
      (error) => error?.status_code === 501 && String(error).toLowerCase().includes("unsupported"),
    );
  });
}
