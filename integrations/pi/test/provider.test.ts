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

async function setup(
	handler: (url: string, init: RequestInit) => Response | Promise<Response>,
	env: Record<string, string> = {},
	extra: { store?: any; prefetchTimeoutMs?: number; credentials?: any } = {},
) {
	const piAi = await import("@earendil-works/pi-ai");
	const { azizamProvider } = await import("../src/provider.ts");
	const calls: { url: string; authorization?: string }[] = [];
	const fetchImpl = (async (url: string, init: RequestInit) => {
		calls.push({ url, authorization: (init.headers as Record<string, string>).authorization });
		return handler(url, init);
	}) as unknown as typeof fetch;
	const store = extra.store ?? new piAi.InMemoryModelsStore();
	const credentials = extra.credentials ?? {
		read: async () => ({ type: "api_key" as const, key: KEY }),
		list: async () => [],
		modify: async (_id: string, fn: any) => fn({ type: "api_key", key: KEY }),
		delete: async () => {},
	};
	const provider = azizamProvider({ env: { AZIZAM_BASE_URL: "http://gw.test/v1", ...env }, fetch: fetchImpl, prefetchTimeoutMs: extra.prefetchTimeoutMs });
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
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST));
	await provider.prefetch(models);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].authorization, `Bearer ${KEY}`);
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

// --- Cold start must not depend on model storage (a failed restore is a miss; persistence is best effort) ---

const piAiStore = async (overrides: Record<string, unknown>) => {
	const piAi = await import("@earendil-works/pi-ai");
	return Object.assign(new piAi.InMemoryModelsStore(), overrides);
};
const denied = async () => { throw new Error("EACCES: models-store.json"); };

test("prefetch serves fetched models when the stored catalog cannot be read", { skip }, async () => {
	const store = await piAiStore({ read: denied });
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { store });
	await provider.prefetch(models);
	assert.equal(calls.length, 1);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	assert.deepEqual(ids(provider.getAllModels!()), ["chat-small", "coder"]);
});

test("prefetch serves fetched models when the catalog cannot be persisted", { skip }, async () => {
	const store = await piAiStore({ write: denied });
	const { models, provider } = await setup(() => json(CONTRACT_LIST), {}, { store });
	await provider.prefetch(models);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	// Pi's own later refresh cannot publish either; the models stay available from the fetch.
	await models.refresh();
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
});

test("a malformed store is a cache miss: prefetch still fetches", { skip }, async () => {
	const { models, provider, store, calls } = await setup(() => json(CONTRACT_LIST));
	await store.write("azizam", { models: "garbage" } as any);
	await provider.prefetch(models);
	assert.equal(calls.length, 1);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
});

test("authoritative empty catalogs stay empty even when persistence fails", { skip }, async () => {
	const store = await piAiStore({ write: denied, read: denied });
	const { models, provider } = await setup(() => json({ data: [{ id: "dall-e", type: "image" }] }), {}, { store });
	await provider.prefetch(models);
	assert.deepEqual(provider.getModels(), []);
});

test("a later successful fetch replaces an earlier one, including with fewer models", { skip }, async () => {
	let body: unknown = CONTRACT_LIST;
	const { models, provider } = await setup(() => json(body), {}, { store: await piAiStore({ write: denied }) });
	await provider.prefetch(models);
	body = { data: [{ id: "only", type: "language" }] };
	await models.refresh();
	assert.deepEqual(ids(provider.getModels()), ["only"]);
});

// --- The cold-start budget covers restore, credential lookup and fetch ---

const never = <T,>() => new Promise<T>(() => {});
const within = async (limitMs: number, work: Promise<unknown>) => {
	const started = Date.now();
	await work;
	const took = Date.now() - started;
	assert.ok(took < limitMs, `took ${took} ms, limit ${limitMs} ms`);
};

test("prefetch is bounded when the stored-catalog restore hangs", { skip }, async () => {
	const store = await piAiStore({ read: never });
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { store, prefetchTimeoutMs: 300 });
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 0, "a spent budget skips the fetch");
});

test("prefetch is bounded when credential resolution is slow, and does not fetch afterwards", { skip }, async () => {
	const credentials = {
		read: () => never(),
		list: async () => [],
		modify: async () => undefined,
		delete: async () => {},
	};
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { credentials, prefetchTimeoutMs: 300 });
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 0);
});

test("prefetch is bounded when the gateway never answers", { skip }, async () => {
	const { models, provider, calls } = await setup(
		(_url, init) => new Promise<Response>((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal!.reason))),
		{},
		{ prefetchTimeoutMs: 300 },
	);
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 1);
	assert.deepEqual(provider.getModels(), []);
});

test("the budget is shared: slow restore plus a fetch cannot exceed it", { skip }, async () => {
	const store = await piAiStore({ read: async () => { await new Promise((r) => setTimeout(r, 200)); return undefined; } });
	const { models, provider } = await setup(
		(_url, init) => new Promise<Response>((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal!.reason))),
		{},
		{ store, prefetchTimeoutMs: 400 },
	);
	await within(700, provider.prefetch(models));
});

test("prefetch falls back to AZIZAM_API_KEY when Pi's credential store cannot be read", { skip }, async () => {
	const credentials = {
		read: async () => { throw new Error("EACCES: auth.json"); },
		list: async () => [],
		modify: async () => undefined,
		delete: async () => {},
	};
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), { AZIZAM_API_KEY: "env-key-1" }, { credentials });
	await provider.prefetch(models);
	assert.equal(calls[0].authorization, "Bearer env-key-1");
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);

	const none = await setup(() => assert.fail("must not fetch"), {}, { credentials });
	await none.provider.prefetch(none.models);
	assert.equal(none.calls.length, 0);
});
