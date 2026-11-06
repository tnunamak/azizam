import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_TOKENS, parseModelList } from "../src/catalog.ts";
import { CONTRACT_LIST } from "./mock-gateway.ts";

test("contract-typed list keeps exactly the language models", () => {
	const catalog = parseModelList(CONTRACT_LIST);
	assert.equal(catalog.mode, "contract");
	assert.deepEqual(catalog.models.map((m) => m.id), ["coder", "chat-small"]);
	const coder = catalog.models[0];
	assert.deepEqual(coder.input, ["text", "image"]);
	assert.equal(coder.reasoning, true);
	assert.equal(coder.contextWindow, 200000);
	assert.equal(coder.maxTokens, 16000);
});

test("defaults apply when optional fields are absent", () => {
	const small = parseModelList(CONTRACT_LIST).models[1];
	assert.deepEqual(small.input, ["text"]);
	assert.equal(small.reasoning, false);
	assert.equal(small.contextWindow, DEFAULT_CONTEXT_WINDOW);
	assert.equal(small.maxTokens, DEFAULT_MAX_TOKENS);
	assert.deepEqual(small.cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
});

test("untyped gateway: output_modalities exactly [text] is chat", () => {
	const catalog = parseModelList({
		data: [
			{ id: "a", architecture: { input_modalities: ["text"], output_modalities: ["text"] } },
			{ id: "img", architecture: { input_modalities: ["text"], output_modalities: ["image"] } },
			{ id: "multi", architecture: { input_modalities: ["text"], output_modalities: ["text", "image"] } },
			{ id: "noarch" },
		],
	});
	assert.equal(catalog.mode, "output-modalities");
	assert.deepEqual(catalog.models.map((m) => m.id), ["a"]);
});

test("untyped gateway: non-string extra output modalities are not text-only", () => {
	const ids = parseModelList({
		data: [
			{ id: "ok", architecture: { output_modalities: ["text"] } },
			{ id: "null", architecture: { output_modalities: ["text", null] } },
			{ id: "obj", architecture: { output_modalities: ["text", {}] } },
		],
	}).models.map((m) => m.id);
	assert.deepEqual(ids, ["ok"]);
});

test("metadata-free gateway lists everything", () => {
	const catalog = parseModelList({ data: [{ id: "gpt-x", object: "model" }, { id: "gpt-y" }] });
	assert.equal(catalog.mode, "unfiltered");
	assert.deepEqual(catalog.models.map((m) => m.id), ["gpt-x", "gpt-y"]);
});

test("mixed typed/untyped: untyped entries are unknown and excluded", () => {
	const catalog = parseModelList({ data: [{ id: "a", type: "language" }, { id: "b" }] });
	assert.deepEqual(catalog.models.map((m) => m.id), ["a"]);
});

test("modality mapping: intersect with text/image, default text", () => {
	const m = (input_modalities: unknown) =>
		parseModelList({ data: [{ id: "x", type: "language", architecture: { input_modalities } }] }).models[0].input;
	assert.deepEqual(m(["text", "image", "audio", "video", "file"]), ["text", "image"]);
	assert.deepEqual(m(["image"]), ["image"]);
	assert.deepEqual(m(["audio", "image"]), ["image"]);
	assert.deepEqual(m(["image", "text"]), ["text", "image"]);
	assert.deepEqual(m(["audio"]), ["text"]);
	assert.deepEqual(m([]), ["text"]);
	assert.deepEqual(m("image"), ["text"]);
	assert.deepEqual(m(undefined), ["text"]);
});

test("reasoning comes from tags only", () => {
	const r = (extra: object) => parseModelList({ data: [{ id: "x", type: "language", ...extra }] }).models[0].reasoning;
	assert.equal(r({ tags: ["reasoning"] }), true);
	assert.equal(r({ tags: ["tool-use", "vision"] }), false);
	assert.equal(r({ tags: "reasoning" }), false);
	assert.equal(r({}), false);
});

test("context window / max tokens: invalid values fall back, max never exceeds context", () => {
	const get = (extra: object) => parseModelList({ data: [{ id: "x", type: "language", ...extra }] }).models[0];
	assert.equal(get({ context_window: "big" }).contextWindow, DEFAULT_CONTEXT_WINDOW);
	assert.equal(get({ context_window: -5 }).contextWindow, DEFAULT_CONTEXT_WINDOW);
	assert.equal(get({ max_tokens: 0 }).maxTokens, DEFAULT_MAX_TOKENS);
	assert.equal(get({ context_window: 4096 }).maxTokens, 4096);
	assert.equal(get({ context_window: 4096, max_tokens: 99999 }).maxTokens, 4096);
	// Positive values that floor to zero, and unsafe integers, are invalid too.
	assert.equal(get({ context_window: 0.5 }).contextWindow, DEFAULT_CONTEXT_WINDOW);
	assert.equal(get({ max_tokens: 0.5 }).maxTokens, DEFAULT_MAX_TOKENS);
	assert.equal(get({ context_window: 2 ** 60 }).contextWindow, DEFAULT_CONTEXT_WINDOW);
	assert.equal(get({ context_window: 4096.7 }).contextWindow, 4096);
});

test("cost is zero unless pricing is provided (per-token -> per-million)", () => {
	const cost = (pricing: unknown) => parseModelList({ data: [{ id: "x", type: "language", pricing }] }).models[0].cost;
	assert.deepEqual(cost({ input: "0.000003", output: 0.000015, input_cache_read: "0.0000003" }), {
		input: 3, output: 15, cacheRead: 0.3, cacheWrite: 0,
	});
	assert.deepEqual(cost({ prompt: "0.000001", completion: "0.000002" }), { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 });
	assert.deepEqual(cost({ input: "free", output: -1 }), { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
	// A finite price that overflows when scaled to per-million is invalid, not Infinity.
	assert.deepEqual(cost({ input: 1e308, output: "1e308" }), { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
});

test("malformed entries are skipped, not fatal; duplicates collapse", () => {
	const catalog = parseModelList({
		data: [null, 7, "str", [], {}, { id: "" }, { id: 5 }, { id: "ok", type: "language", tags: [1, null] }, { id: "ok", type: "language" }],
	});
	assert.deepEqual(catalog.models.map((m) => m.id), ["ok"]);
	assert.equal(catalog.malformed, 8);
});

test("non-list bodies throw a clear error", () => {
	for (const body of [null, [], "x", {}, { data: "no" }]) assert.throws(() => parseModelList(body), /no `data` array/);
});

test("cache-write pricing is mapped and an explicit zero price wins over aliases", () => {
	const model = parseModelList({
		data: [{
			id: "priced",
			type: "language",
			pricing: {
				input: 0,
				prompt: "0.000003",
				output: 0,
				completion: "0.000004",
				input_cache_read: "0.000001",
				input_cache_write: "0.000002",
			},
		}],
	}).models[0];
	assert.deepEqual(model.cost, { input: 0, output: 0, cacheRead: 1, cacheWrite: 2 });
});
