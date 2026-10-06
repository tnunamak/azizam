// Provider-level tests against pi-ai's real createModels(), using an in-memory store and a fake fetch.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CONTRACT_LIST } from "./mock-gateway.ts";
import { piAiRoot, registerPiResolver } from "./pi-resolve.ts";

const root = piAiRoot();
if (root) registerPiResolver(root);
const skip = root ? false : "pi-ai not found (install Pi or set PI_BIN)";
const KEY = "unit-test-key-77";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

async function setup(handler: (url: string, init: RequestInit) => Response | Promise<Response>, env: Record<string, string> = {}) {
	const piAi = await import("@earendil-works/pi-ai");
	const { azizamProvider } = await import("../src/provider.ts");
	const calls: { url: string; authorization?: string }[] = [];
	const fetchImpl = (async (url: string, init: RequestInit) => {
		calls.push({ url, authorization: (init.headers as Record<string, string>).authorization });
		return handler(url, init);
	}) as unknown as typeof fetch;
	const store = new piAi.InMemoryModelsStore();
	const credentials = {
		read: async () => ({ type: "api_key" as const, key: KEY }),
		list: async () => [],
		modify: async (_id: string, fn: any) => fn({ type: "api_key", key: KEY }),
		delete: async () => {},
	};
	const provider = azizamProvider({ env: { AZIZAM_BASE_URL: "http://gw.test/v1", ...env }, fetch: fetchImpl });
	const models = piAi.createModels({ credentials, modelsStore: store });
	models.setProvider(provider);
	return { models, store, calls, provider };
}
const ids = (models: readonly { id: string }[]) => models.map((m) => m.id).sort();

test("refresh publishes the language models, sends the stored/env key, and persists", { skip }, async () => {
	const { models, store, calls } = await setup(() => json(CONTRACT_LIST));
	const result = await models.refresh();
	assert.equal(result.errors.size, 0);
	assert.deepEqual(ids(models.getModels("azizam")), ["chat-small", "coder"]);
	assert.equal(calls[0].url, "http://gw.test/v1/models");
	assert.equal(calls[0].authorization, `Bearer ${KEY}`);
	const stored = await store.read("azizam");
	assert.deepEqual(ids(stored!.models), ["chat-small", "coder"]);
	const coder = models.getModel("azizam", "coder")!;
	assert.equal(coder.api, "openai-completions");
	assert.deepEqual((coder as any).compat, {
		supportsDeveloperRole: false, supportsReasoningEffort: true, supportsStrictMode: false, maxTokensField: "max_tokens", thinkingFormat: "openai",
	});
});

test("failed refresh keeps the last good catalog and reports an error without the key", { skip }, async () => {
	let fail = false;
	const { models } = await setup(() => (fail ? json({ error: "x" }, 401) : json(CONTRACT_LIST)));
	await models.refresh();
	fail = true;
	const result = await models.refresh();
	const error = result.errors.get("azizam");
	assert.ok(error, "expected an error");
	assert.match(error.message, /rejected the API key/);
	assert.ok(!error.message.includes(KEY));
	assert.deepEqual(ids(models.getModels("azizam")), ["chat-small", "coder"]);
});

test("persisted catalog is restored on offline start and rebased to the current URL", { skip }, async () => {
	const first = await setup(() => json(CONTRACT_LIST));
	await first.models.refresh();
	const stored = await first.store.read("azizam");

	const second = await setup(() => assert.fail("offline start must not fetch"), { AZIZAM_BASE_URL: "http://other.test/v1" });
	await second.store.write("azizam", stored!);
	const result = await second.models.refresh({ allowNetwork: false });
	assert.equal(result.errors.size, 0);
	assert.deepEqual(ids(second.models.getModels("azizam")), ["chat-small", "coder"]);
	assert.equal(second.models.getModel("azizam", "coder")!.baseUrl, "http://other.test/v1");
	assert.equal(second.calls.length, 0);
});

test("invalid AZIZAM_BASE_URL is reported as an error, not a crash", { skip }, async () => {
	const { models } = await setup(() => json(CONTRACT_LIST), { AZIZAM_BASE_URL: "nonsense" });
	const result = await models.refresh();
	assert.match(result.errors.get("azizam")?.message ?? "", /not a valid URL/);
});

test("prefetch seeds models for first run; skipped offline or without a key; never throws", { skip }, async () => {
	const seeded = await setup(() => json(CONTRACT_LIST), { AZIZAM_API_KEY: KEY });
	await seeded.provider.prefetch();
	assert.deepEqual(ids(seeded.provider.getModels()), ["chat-small", "coder"]);
	assert.equal(seeded.calls[0].authorization, `Bearer ${KEY}`);

	for (const env of [{}, { AZIZAM_API_KEY: KEY, PI_OFFLINE: "1" }]) {
		const skipped = await setup(() => assert.fail("must not fetch"), env);
		await skipped.provider.prefetch();
		assert.equal(skipped.calls.length, 0);
		assert.deepEqual(skipped.provider.getModels(), []);
	}

	const broken = await setup(() => { throw new Error("boom"); }, { AZIZAM_API_KEY: KEY });
	await broken.provider.prefetch();
	assert.deepEqual(broken.provider.getModels(), []);
});
