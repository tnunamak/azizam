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
	extra: { store?: any; prefetchTimeoutMs?: number; credentials?: any; configuredBaseUrl?: () => Promise<string | undefined> } = {},
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
	const provider = azizamProvider({ env: { AZIZAM_BASE_URL: "http://gw.test/v1", ...env }, fetch: fetchImpl, prefetchTimeoutMs: extra.prefetchTimeoutMs, configuredBaseUrl: extra.configuredBaseUrl, modelsStore: store });
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

test("slow discovery on a cold start without a cache waits past the former 3 second limit", { skip }, async () => {
	const { models, provider } = await setup(async (_url, init) => {
		await new Promise<void>((resolve, reject) => {
			const timer = setTimeout(resolve, 3_500);
			init.signal!.addEventListener("abort", () => {
				clearTimeout(timer);
				reject(init.signal!.reason);
			}, { once: true });
		});
		return json(CONTRACT_LIST);
	});
	await provider.prefetch(models);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
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

test("prefetch attempts persistence and retains fetched models when that write fails", { skip }, async (t) => {
	const errors: string[] = [];
	t.mock.method(console, "error", (...args: unknown[]) => { errors.push(args.map(String).join(" ")); });
	const writes: { providerId: string; entry: any; signal?: AbortSignal }[] = [];
	const store = await piAiStore({
		write: async (providerId: string, entry: any, options?: { signal?: AbortSignal }) => {
			writes.push({ providerId, entry, signal: options?.signal });
			throw new Error("synthetic write denied");
		},
	});
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { store });
	await provider.prefetch(models);
	assert.equal(calls.length, 1);
	assert.equal(writes.length, 1, "the prefetch persistence branch must execute");
	assert.equal(writes[0].providerId, "azizam");
	assert.deepEqual(ids(writes[0].entry.models), ["chat-small", "coder"]);
	assert.ok(Number.isFinite(writes[0].entry.checkedAt));
	assert.ok(writes[0].signal instanceof AbortSignal);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
	assert.deepEqual(ids(provider.getAllModels!()), ["chat-small", "coder"]);
	assert.ok(errors.every((message) => !message.includes(KEY)));
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

const stalledCredentials = {
	read: () => never(),
	list: async () => [],
	modify: async () => undefined,
	delete: async () => {},
};

test("a stalled model store cannot starve discovery: the key still resolves and the fetch runs", { skip }, async () => {
	const store = await piAiStore({ read: never });
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { store, prefetchTimeoutMs: 400 });
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 1);
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
});

test("a stalled credential store plus a valid AZIZAM_API_KEY still discovers models", { skip }, async () => {
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), { AZIZAM_API_KEY: "env-key-2" }, { credentials: stalledCredentials, prefetchTimeoutMs: 400 });
	await within(900, provider.prefetch(models));
	assert.equal(calls[0]?.authorization, "Bearer env-key-2");
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
});

test("stalled model store and stalled credential store together still discover with the env key", { skip }, async () => {
	const store = await piAiStore({ read: never });
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), { AZIZAM_API_KEY: "env-key-3" }, { store, credentials: stalledCredentials, prefetchTimeoutMs: 400 });
	await within(900, provider.prefetch(models));
	assert.equal(calls[0]?.authorization, "Bearer env-key-3");
	assert.deepEqual(ids(provider.getModels()), ["chat-small", "coder"]);
});

test("a stalled credential store without an env key is bounded and does not fetch", { skip }, async () => {
	const { models, provider, calls } = await setup(() => json(CONTRACT_LIST), {}, { credentials: stalledCredentials, prefetchTimeoutMs: 300 });
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 0);
});

test("prefetch is bounded when the gateway never answers and reports the cache miss without debug", { skip }, async (t) => {
	const errors: string[] = [];
	t.mock.method(console, "error", (message: string) => errors.push(message));
	const { models, provider, calls } = await setup(
		(_url, init) => new Promise<Response>((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(init.signal!.reason))),
		{},
		{ prefetchTimeoutMs: 300 },
	);
	await within(900, provider.prefetch(models));
	assert.equal(calls.length, 1);
	assert.deepEqual(provider.getModels(), []);
	assert.equal(errors.length, 1);
	assert.match(errors[0], /exceeded the 300 ms startup budget; no saved catalog/);
	assert.match(errors[0], /retry Pi/);
	assert.ok(!errors[0].includes(KEY));
});

test("a failed first discovery reports an error without echoing untrusted exception text", { skip }, async (t) => {
	const errors: string[] = [];
	t.mock.method(console, "error", (message: string) => errors.push(message));
	const { models, provider } = await setup(() => { throw new Error(`request failed with ${KEY}`); });
	await provider.prefetch(models);
	assert.deepEqual(provider.getModels(), []);
	assert.equal(errors.length, 1);
	assert.match(errors[0], /Model discovery failed; no saved catalog/);
	assert.ok(!errors[0].includes(KEY));
});

test("restore, lookup and fetch share one controlled startup deadline", { skip, timeout: 10000 }, async (t) => {
	const { azizamProvider } = await import("../src/provider.ts");
	const deferred = () => {
		let resolve!: () => void;
		const promise = new Promise<void>((done) => { resolve = done; });
		return { promise, resolve };
	};
	const restoreEntered = deferred();
	const lookupEntered = deferred();
	const fetchEntered = deferred();
	const deadlines: { ms: number; controller: AbortController }[] = [];
	let restoreSignal: AbortSignal | undefined;
	let lookupSignal: AbortSignal | undefined;
	let fetchSignal: AbortSignal | undefined;
	const fetchCalls: { url: string; authorization: string | null }[] = [];

	const fetcher: typeof fetch = async (url, init) => {
		fetchSignal = init?.signal ?? undefined;
		fetchCalls.push({ url: String(url), authorization: new Headers(init?.headers).get("authorization") });
		fetchEntered.resolve();
		return new Promise<Response>((_resolve, reject) => {
			const signal = fetchSignal!;
			const abort = () => reject(signal.reason);
			if (signal.aborted) abort();
			else signal.addEventListener("abort", abort, { once: true });
		});
	};
	const provider = azizamProvider({
		env: { AZIZAM_BASE_URL: "http://gw.test/v1", AZIZAM_API_KEY: "budget-env-key" },
		prefetchTimeoutMs: 400,
		fetch: fetcher,
	});
	const runtime = {
		refresh: async (options: any) => {
			assert.equal(options.allowNetwork, false);
			assert.deepEqual(options.providers, ["azizam"]);
			restoreSignal = options.signal;
			restoreEntered.resolve();
			return new Promise<never>(() => {});
		},
		getAuth: async (providerId: string, options: any) => {
			assert.equal(providerId, "azizam");
			lookupSignal = options.signal;
			lookupEntered.resolve();
			return new Promise<never>(() => {});
		},
	} as unknown as Parameters<typeof provider.prefetch>[0];

	t.mock.method(AbortSignal, "timeout", (ms: number) => {
		const controller = new AbortController();
		deadlines.push({ ms, controller });
		return controller.signal;
	});
	t.mock.method(console, "error", () => {});
	const pending = provider.prefetch(runtime);
	try {
		await restoreEntered.promise;
		assert.deepEqual(deadlines.map((item) => item.ms), [400, 100]);
		deadlines[1].controller.abort(new DOMException("restore slice", "TimeoutError"));

		await lookupEntered.promise;
		assert.equal(restoreSignal?.aborted, true);
		assert.deepEqual(deadlines.map((item) => item.ms), [400, 100, 100]);
		deadlines[2].controller.abort(new DOMException("lookup slice", "TimeoutError"));

		await fetchEntered.promise;
		assert.equal(lookupSignal?.aborted, true);
		assert.deepEqual(fetchCalls, [{ url: "http://gw.test/v1/models", authorization: "Bearer budget-env-key" }]);
		assert.equal(fetchSignal?.aborted, false);
		deadlines[0].controller.abort(new DOMException("overall budget", "TimeoutError"));

		assert.equal(fetchSignal?.aborted, true, "the original overall deadline must abort the in-flight fetch immediately");
		await pending;
		assert.deepEqual(provider.getModels(), []);
	} finally {
		for (const { controller } of deadlines) {
			if (!controller.signal.aborted) controller.abort(new DOMException("test cleanup", "AbortError"));
		}
		await pending;
	}
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

test("cold start reports missing credentials separately from lookup failure", { skip }, async (t) => {
	const errors: string[] = [];
	t.mock.method(console, "error", (message: string) => errors.push(message));
	const missing = { read: async () => undefined, list: async () => [], modify: async () => undefined, delete: async () => {} };
	const first = await setup(() => assert.fail("must not fetch"), {}, { credentials: missing });
	await first.provider.prefetch(first.models);
	assert.match(errors.pop() ?? "", /Missing Azizam credential.*AZIZAM_API_KEY.*\/login azizam.*models.json/);
	const broken = { ...missing, read: async () => { throw new Error(`lookup failed ${KEY}`); } };
	const second = await setup(() => assert.fail("must not fetch"), {}, { credentials: broken });
	await second.provider.prefetch(second.models);
	const message = errors.pop() ?? "";
	assert.match(message, /Azizam credential lookup failed.*AZIZAM_API_KEY.*\/login azizam.*models.json/);
	assert.ok(!message.includes(KEY));
});

test("ordinary refresh blocks conflicting URL sources before sending a credential", { skip }, async () => {
	const { models, calls } = await setup(() => json(CONTRACT_LIST), {}, { configuredBaseUrl: async () => "http://configured.test/v1" });
	const result = await models.refresh();
	assert.equal(calls.length, 0);
	assert.match(result.errors.get("azizam")?.message ?? "", /AZIZAM_BASE_URL.*models.json.*conflict/);
});

test("ordinary refresh never surfaces a credential from a transport cause", { skip }, async () => {
	const { models } = await setup(() => { throw new TypeError("fetch failed", { cause: Object.assign(new Error(`request contained ${KEY}`), { code: "ECONNREFUSED" }) }); });
	const message = (await models.refresh()).errors.get("azizam")?.message ?? "";
	assert.match(message, /connection was refused/);
	assert.ok(!message.includes(KEY));
});
