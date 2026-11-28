import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_TOKENS, parseModelList } from "../src/catalog.ts";
import { DEFAULT_BASE_URL, normalizeBaseUrl } from "../src/gateway.ts";
import { CONTRACT_LIST } from "./mock-gateway.ts";

test("an unset or whitespace-only catalog URL uses the same default", () => {
	assert.equal(normalizeBaseUrl(undefined), DEFAULT_BASE_URL);
	assert.equal(normalizeBaseUrl("  \t "), DEFAULT_BASE_URL);
});

test("catalog URL normalization preserves the path and removes trailing slashes", () => {
	assert.equal(normalizeBaseUrl("  https://example.invalid/custom/v1///  "), "https://example.invalid/custom/v1");
});

test("relative gateway URLs give the operator an absolute URL example", () => {
	assert.throws(() => normalizeBaseUrl("/relative/v1"), (error: unknown) =>
		error instanceof Error && error.name === "AzizamError" &&
		error.message.includes("is not a valid URL") && error.message.includes(DEFAULT_BASE_URL));
});

test("a non-HTTP gateway URL is rejected with the supported schemes", () => {
	assert.throws(() => normalizeBaseUrl("ftp://example.invalid/v1"), {
		name: "AzizamError",
		message: "AZIZAM_BASE_URL must start with http:// or https://.",
	});
});

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

test("discarded rows do not change the mode of a usable legacy catalog", () => {
	const catalog = parseModelList({
		data: [{ type: "image" }, { id: "legacy", object: "model" }],
	});
	assert.equal(catalog.mode, "unfiltered");
	assert.deepEqual(catalog.models.map((model) => model.id), ["legacy"]);
	assert.equal(catalog.malformed, 1);
});

test("declared language type takes precedence over secondary output metadata", () => {
	const catalog = parseModelList({
		data: [{
			id: "described",
			type: "language",
			architecture: { input_modalities: ["image"], output_modalities: ["text", "image"] },
		}],
	});
	assert.equal(catalog.mode, "contract");
	assert.deepEqual(catalog.models.map((model) => [model.id, model.input]), [["described", ["image"]]]);
	assert.equal(catalog.malformed, 0);
});

test("mixed typed/untyped: untyped entries are unknown and excluded", () => {
	const catalog = parseModelList({ data: [{ id: "a", type: "language" }, { id: "b" }] });
	assert.deepEqual(catalog.models.map((m) => m.id), ["a"]);
});

test("malformed legacy output hints do not hide usable text models", () => {
	const catalog = parseModelList({ data: [
		{ id: "bad-hint", architecture: { output_modalities: "text" } },
		{ id: "text-model", architecture: { output_modalities: ["text"] } },
		{ id: "bad-architecture", architecture: [] },
	] });
	assert.equal(catalog.mode, "output-modalities");
	assert.deepEqual(catalog.models.map((model) => model.id), ["text-model"]);
});

test("a non-string type marker does not override legacy output metadata", () => {
	const catalog = parseModelList({ data: [
		{ id: "legacy-typed-number", type: 7, architecture: { output_modalities: ["text"] } },
		{ id: "legacy-image", architecture: { output_modalities: ["image"] } },
	] });

	assert.equal(catalog.mode, "output-modalities");
	assert.equal(catalog.malformed, 0);
	assert.deepEqual(catalog.models.map(({ id }) => id), ["legacy-typed-number"]);
});

test("input badges deduplicate exact modality labels without normalizing unknown variants", () => {
	const { models } = parseModelList({ data: [{
		id: "picture-fixture",
		type: "language",
		architecture: { input_modalities: ["image", "TEXT", "image"] },
	}] });

	assert.deepEqual(models.map(({ id, input }) => [id, input]), [["picture-fixture", ["image"]]]);
});

test("a partially malformed pricing descriptor preserves valid sibling estimates", () => {
	const model = parseModelList({ data: [{
		id: "estimate-model",
		type: "language",
		pricing: { input: null, prompt: "0.000002", output: "", input_cache_read: -1, input_cache_write: "0.000003" },
	}] }).models[0];
	assert.deepEqual(model.cost, { input: 2, output: 0, cacheRead: 0, cacheWrite: 3 });
});

test("modality mapping: intersect with text/image, default text", () => {
	const m = (input_modalities: unknown) =>
		parseModelList({ data: [{ id: "x", type: "language", architecture: { input_modalities } }] }).models[0].input;
	assert.deepEqual(m(["text", "image", "audio", "video", "file"]), ["text", "image"]);
	assert.deepEqual(m(["image"]), ["image"]);
	assert.deepEqual(m(["audio", "image"]), ["image"]);
	assert.deepEqual(m(["image", "text"]), ["text", "image"]);
	assert.deepEqual(m([null, 3]), ["text"]);
	assert.deepEqual(m([]), ["text"]);
	assert.deepEqual(m("image"), ["text"]);
	assert.deepEqual(m(undefined), ["text"]);
});

test("an explicitly unsupported input list excludes the descriptor without inventing text", () => {
	const unsupported = { id: "fixture-audio", type: "language", architecture: { input_modalities: ["audio"] } };
	const catalog = parseModelList({
		data: [unsupported, { id: "fixture-text", type: "language" }, unsupported],
	});
	assert.equal(catalog.mode, "contract");
	assert.equal(catalog.malformed, 1); // The duplicate, not unsupported input metadata.
	assert.deepEqual(catalog.models.map(({ id, input }) => [id, input]), [["fixture-text", ["text"]]]);
	const legacy = parseModelList({
		data: [{ id: "fixture-audio", architecture: { output_modalities: ["text"], input_modalities: ["audio"] } }],
	});
	assert.equal(legacy.mode, "output-modalities");
	assert.equal(legacy.malformed, 0);
	assert.deepEqual(legacy.models, []);
});

test("an unsupported descriptor does not hide the first usable duplicate", () => {
	const model = (input: string) => ({
		id: "m",
		type: "language",
		architecture: { input_modalities: [input] },
	});
	const catalog = parseModelList({
		data: [model("audio"), model("text"), model("image")],
	});
	assert.deepEqual(catalog.models.map(({ id, input }) => [id, input]), [["m", ["text"]]]);
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

test("whitespace-only model ids are malformed without rewriting other ids", () => {
	const catalog = parseModelList({ data: [
		{ id: " \t\n", type: "language" },
		{ id: " sample-model ", type: "language" },
		{ id: "regular-model", type: "language" },
	] });
	assert.equal(catalog.malformed, 1);
	assert.deepEqual(catalog.models.map((model) => model.id), [" sample-model ", "regular-model"]);
});

test("negative cache-read pricing preserves the model and its other prices", () => {
	const catalog = parseModelList({ data: [{
		id: "fictional-negative-cache-read", type: "language",
		pricing: { input: "0.000002", output: 0.000003, input_cache_read: "-0.1", input_cache_write: "0.000001" },
	}] });
	assert.equal(catalog.malformed, 0);
	assert.deepEqual(catalog.models.map(({ id, cost }) => ({ id, cost })), [{
		id: "fictional-negative-cache-read", cost: { input: 2, output: 3, cacheRead: 0, cacheWrite: 1 },
	}]);
});

test("overflowing cache-write pricing preserves the model and valid cache-read cost", () => {
	const catalog = parseModelList({ data: [{
		id: "fictional-overflow-cache-write", type: "language",
		pricing: { input: 0.000002, output: "0.000003", input_cache_read: "0.000001", input_cache_write: "1e308" },
	}] });
	assert.equal(catalog.malformed, 0);
	assert.deepEqual(catalog.models.map(({ id, cost }) => ({ id, cost })), [{
		id: "fictional-overflow-cache-write", cost: { input: 2, output: 3, cacheRead: 1, cacheWrite: 0 },
	}]);
});

test("a valid empty catalog is not malformed", () => {
	assert.deepEqual(parseModelList({ data: [] }), { mode: "unfiltered", models: [], malformed: 0 });
});

test("an invalid preferred price falls back to a valid legacy price", () => {
	const model = parseModelList({
		data: [{ id: "fixture", pricing: { input: "unknown", prompt: "0.000003", output: -1, completion: "0.000004" } }],
	}).models[0];
	assert.deepEqual(model.cost, { input: 3, output: 4, cacheRead: 0, cacheWrite: 0 });
});

test("an explicit zero input or output price does not fall back to legacy prices", () => {
	const result = parseModelList({
		data: [{ id: "fixture", pricing: { input: 0, prompt: "0.000003", output: "0", completion: "0.000004" } }],
	});
	assert.equal(result.models.length, 1);
	assert.deepEqual(result.models[0].cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
});

test("non-list bodies throw a clear error", () => {
	for (const body of [null, [], "x", {}, { data: "no" }]) assert.throws(() => parseModelList(body), /no `data` array/);
});

test("a list containing only broken rows is empty, with all rows counted as malformed", () => {
	const catalog = parseModelList({ data: [null, { id: "" }, { id: 27 }] });
	assert.deepEqual(catalog, { mode: "unfiltered", models: [], malformed: 3 });
});

test("no supported chat model does not fall back to untyped rows or report them as malformed", () => {
	const catalog = parseModelList({
		data: [
			{ id: "vector-only", type: "embedding" },
			{ id: "unknown", architecture: { output_modalities: ["text"] } },
		],
	});
	assert.deepEqual(catalog, { mode: "contract", models: [], malformed: 0 });
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

test("a typed catalog with no language models does not fall back to text hints", () => {
	const catalog = parseModelList({
		data: [
			{ id: "voice", type: "audio" },
			{ id: "untyped", architecture: { output_modalities: ["text"] } },
		],
	});
	assert.equal(catalog.mode, "contract");
	assert.deepEqual(catalog.models, []);
	assert.equal(catalog.malformed, 0);
});

test("duplicate IDs keep the first descriptor in the original model order", () => {
	const catalog = parseModelList({
		data: [
			{ id: "z", type: "language", context_window: 64_000, tags: ["reasoning"] },
			{ id: "a", type: "language", context_window: 32_000 },
			{ id: "z", type: "language", context_window: 128_000 },
		],
	});
	assert.deepEqual(
		catalog.models.map(({ id, contextWindow, reasoning }) => ({ id, contextWindow, reasoning })),
		[
			{ id: "z", contextWindow: 64_000, reasoning: true },
			{ id: "a", contextWindow: 32_000, reasoning: false },
		],
	);
	assert.equal(catalog.malformed, 1);
});

test("duplicate IDs cannot hide a catalog filter or its eligible descriptor", () => {
	const plain = { id: "shared" };
	const unrelated = { id: "unrelated" };
	const cases = [
		[{ ...plain, type: "language", context_window: 64_000 }, "contract"],
		[{ ...plain, architecture: { output_modalities: ["text"] }, context_window: 64_000 }, "output-modalities"],
	] as const;
	for (const [eligible, mode] of cases) {
		for (const twins of [[plain, eligible], [eligible, plain]]) {
			const catalog = parseModelList({ data: [...twins, unrelated, null] });
			assert.equal(catalog.mode, mode);
			assert.deepEqual(catalog.models.map(({ id, contextWindow }) => ({ id, contextWindow })),
				[{ id: "shared", contextWindow: 64_000 }]);
			assert.equal(catalog.malformed, 2);
		}
	}
});

test("reasoning display recognizes the exact tag despite unrecognized siblings", () => {
	const { models } = parseModelList({ data: [
		{ id: "exact", type: "language", tags: [null, 42, "tool-use", "reasoning"] },
		{ id: "unrecognized", type: "language", tags: ["Reasoning", " reasoning "] },
	] });

	assert.deepEqual(models.map(({ reasoning }) => reasoning), [true, false]);
});

test("a friendly label does not replace the catalog's model identifier", () => {
	const { models } = parseModelList({ data: [
		{ id: "fixture/model-a", type: "language", name: "Same label" },
		{ id: "fixture/model-b", type: "language", name: "Same label" },
	] });

	assert.deepEqual(models.map(({ id, name }) => ({ id, name })), [
		{ id: "fixture/model-a", name: "fixture/model-a" },
		{ id: "fixture/model-b", name: "fixture/model-b" },
	]);
});

test("catalog token display floors explicit fractions but does not coerce string limits", () => {
	const { models } = parseModelList({ data: [
		{ id: "numeric", type: "language", context_window: 20_000, max_tokens: 4096.9 },
		{ id: "string-limit", type: "language", context_window: 20_000, max_tokens: "4096" },
		{ id: "unsafe-limit", type: "language", context_window: 20_000, max_tokens: Number.MAX_SAFE_INTEGER + 1 },
	] });

	assert.deepEqual(models.map(({ id, maxTokens }) => [id, maxTokens]), [
		["numeric", 4096],
		["string-limit", DEFAULT_MAX_TOKENS],
		["unsafe-limit", DEFAULT_MAX_TOKENS],
	]);
});

test("catalog display selects modern and legacy estimates independently per price channel", () => {
	const { models } = parseModelList({ data: [{
		id: "mixed-estimates",
		type: "language",
		pricing: { input: " 0.000003 ", prompt: "0.000009", output: null, completion: "0.000005" },
	}] });

	assert.deepEqual(models[0].cost, { input: 3, output: 5, cacheRead: 0, cacheWrite: 0 });
});
