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

test("prefetch fetches on a cold start with the credential Pi resolves, no AZIZAM_API_KEY in env", { skip }, async () => {
	const { models, provider, calls, store } = await setup(() => json(CONTRACT_LIST));
	await provider.prefetch(models);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].authorization, `Bearer ${KEY}`);
	assert.deepEqual(ids((await store.read("azizam"))!.models), ["chat-small", "coder"]);
});

test("prefetch is skipped offline, never throws, and a failed fetch leaves no models", { skip }, async () => {
	const offline = await setup(() => assert.fail("must not fetch"), { PI_OFFLINE: "1" });
	await offline.provider.prefetch(offline.models);
	assert.equal(offline.calls.length, 0);
	assert.deepEqual(offline.provider.getModels(), []);

	const broken = await setup(() => { throw new Error("boom"); });
	await broken.provider.prefetch(broken.models);
	assert.deepEqual(broken.provider.getModels(), []);

	const noCredential = await setup(() => assert.fail("must not fetch"));
	const missing = { read: async () => undefined, list: async () => [], modify: async () => undefined, delete: async () => {} };
	const piAi = await import("@earendil-works/pi-ai");
	const bare = piAi.createModels({ credentials: missing as any, modelsStore: new piAi.InMemoryModelsStore() });
	bare.setProvider(noCredential.provider);
	await noCredential.provider.prefetch(bare);
	assert.equal(noCredential.calls.length, 0);
});

test("prefetch skips the eager fetch when a persisted catalog for the configured base URL exists", { skip }, async () => {
	const first = await setup(() => json(CONTRACT_LIST));
	await first.models.refresh();
	const stored = await first.store.read("azizam");

	const second = await setup(() => assert.fail("must not fetch"));
	await second.store.write("azizam", stored!);
	await second.provider.prefetch(second.models);
	assert.equal(second.calls.length, 0);
	assert.deepEqual(ids(second.provider.getModels()), ["chat-small", "coder"]);
});

test("prefetch fetches when the persisted catalog is for another base URL or empty; a malformed store is a miss", { skip }, async () => {
	const first = await setup(() => json(CONTRACT_LIST));
	await first.models.refresh();
	const stored = await first.store.read("azizam");

	const otherUrl = await setup(() => json(CONTRACT_LIST), { AZIZAM_BASE_URL: "http://other.test/v1" });
	await otherUrl.store.write("azizam", stored!);
	await otherUrl.provider.prefetch(otherUrl.models);
	assert.equal(otherUrl.calls.length, 1);

	const empty = await setup(() => json(CONTRACT_LIST));
	await empty.store.write("azizam", { models: [] });
	await empty.provider.prefetch(empty.models);
	assert.equal(empty.calls.length, 1);

	const malformed = await setup(() => json(CONTRACT_LIST));
	await malformed.store.write("azizam", { models: "garbage" } as any);
	await malformed.provider.prefetch(malformed.models);
	// Pi's refresh restores the store before any network call, so a store it cannot read blocks fetching too; the
	// prefetch must treat that as a miss (not as a usable catalog) and must not throw.
	assert.deepEqual(malformed.provider.getModels(), []);
});

test("an authoritative empty refresh does not resurrect earlier models", { skip }, async () => {
	let body: unknown = CONTRACT_LIST;
	const { models, provider } = await setup(() => json(body));
	await provider.prefetch(models);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	body = { data: [{ id: "dall-e", type: "image" }] };
	await models.refresh();
	assert.deepEqual(provider.getModels(), []);
	assert.deepEqual(models.getModels("azizam"), []);
});

for (const [name, url] of [
	["unparseable with userinfo", "http://user:topsecret@"],
	["userinfo", "http://user:topsecret@gw.test/v1"],
	["query string", "http://gw.test/v1?api_key=topsecret"],
	["fragment", "http://gw.test/v1#topsecret"],
	["empty query", "http://gw.test/v1?"],
] as const) {
	test(`AZIZAM_BASE_URL with ${name} is rejected without echoing the input`, { skip }, async () => {
		const { models } = await setup(() => assert.fail("must not fetch"), { AZIZAM_BASE_URL: url });
		const result = await models.refresh();
		const message = result.errors.get("azizam")?.message ?? "";
		assert.match(message, /AZIZAM_BASE_URL/);
		assert.ok(!message.includes("topsecret") && !message.includes("api_key"), message);
	});
}

test("errors about an unreachable or failing gateway show only the credential-free URL", { skip }, async () => {
	const { models } = await setup(() => { throw new TypeError("fetch failed"); }, { AZIZAM_BASE_URL: "http://gw.test/v1/" });
	const message = (await models.refresh()).errors.get("azizam")?.message ?? "";
	assert.match(message, /http:\/\/gw\.test\/v1[^/]/);
});
