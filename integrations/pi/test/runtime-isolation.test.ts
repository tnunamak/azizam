// The extension's throwaway Pi ModelRuntime must resolve only azizam's own credential: credential commands of other
// providers (password-manager `!command` keys) must not run because azizam registered. Uses Pi's real ModelRuntime.
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { CONTRACT_LIST } from "./mock-gateway.ts";

const root = mkdtempSync(join(homedir(), ".tmp", "pi-azizam-isolation-"));
after(() => rmSync(root, { recursive: true, force: true }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
const runs = (name: string) => (existsSync(join(root, name)) ? readFileSync(join(root, name), "utf8").split("\n").filter(Boolean).length : 0);

/** A fresh agent dir where unrelated providers hold `!command` keys and azizam's key is a `models.json` command. */
function agentDir(name: string): string {
	const dir = join(root, name);
	mkdirSync(dir, { recursive: true });
	const counter = (label: string, key: string) => `!echo ${label} >> ${join(root, `${name}.${label}`)}; echo ${key}`;
	writeFileSync(join(dir, "auth.json"), JSON.stringify({
		openai: { type: "api_key", key: counter("openai", "k-openai") },
		anthropic: { type: "api_key", key: counter("anthropic", "k-anthropic") },
	}));
	writeFileSync(join(dir, "models.json"), JSON.stringify({ providers: { azizam: { apiKey: counter("azizam", "k-azizam") } } }));
	return dir;
}

async function boot(name: string, register: "scoped" | "pi-default") {
	process.env.PI_CODING_AGENT_DIR = agentDir(name);
	delete process.env.PI_OFFLINE;
	const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");
	const { azizamProvider, registerProviderScoped } = await import("../src/provider.ts");
	const auth: (string | undefined)[] = [];
	const provider = azizamProvider({
		env: { AZIZAM_BASE_URL: "http://gw.test/v1" },
		fetch: (async (_url: string, init: RequestInit) => {
			auth.push((init.headers as Record<string, string>).authorization);
			return json(CONTRACT_LIST);
		}) as unknown as typeof fetch,
	});
	const runtime = await ModelRuntime.create({ refreshOnCreate: false });
	const refreshes: Promise<unknown>[] = [];
	const refresh = runtime.refresh.bind(runtime);
	(runtime as any).refresh = (...args: Parameters<typeof refresh>) => {
		const pending = refresh(...args);
		refreshes.push(pending);
		return pending;
	};
	if (register === "scoped") registerProviderScoped(runtime, provider);
	else runtime.registerNativeProvider(provider);
	await Promise.all(refreshes);
	await provider.prefetch(runtime);
	// Registration and prefetch can queue refresh work on the next turn. Drain
	// every observed refresh and one event-loop turn before checking commands.
	for (;;) {
		const pending = refreshes.splice(0);
		await Promise.all(pending);
		await new Promise<void>((resolve) => setImmediate(resolve));
		if (refreshes.length === 0) break;
	}
	return { runtime, provider, auth };
}

test("control: Pi's own registerNativeProvider runs other providers' credential commands", async () => {
	await boot("control", "pi-default");
	assert.ok(runs("control.openai") + runs("control.anthropic") > 0, "expected Pi's global refresh to resolve other providers");
});

test("scoped registration runs no credential command of any other provider", async () => {
	const { auth, provider } = await boot("scoped", "scoped");
	assert.equal(runs("scoped.openai"), 0);
	assert.equal(runs("scoped.anthropic"), 0);
	assert.deepEqual(provider.getModels().map((m) => m.id).sort(), ["chat-small", "coder"]);
	assert.deepEqual(auth, ["Bearer k-azizam"]);
});

test("scoped registration runs azizam's own models.json command once", async () => {
	await boot("own", "scoped");
	assert.equal(runs("own.azizam"), 1);
});

test("registerProviderScoped restores the runtime's refresh and swallows only the unscoped one", async () => {
	process.env.PI_CODING_AGENT_DIR = agentDir("restore");
	const { ModelRuntime } = await import("@earendil-works/pi-coding-agent");
	const { azizamProvider, registerProviderScoped } = await import("../src/provider.ts");
	const runtime = await ModelRuntime.create({ refreshOnCreate: false });
	const seen: unknown[] = [];
	const spy = runtime.refresh.bind(runtime);
	(runtime as any).refresh = async (options: any) => { seen.push(options?.providers ?? "all"); return spy(options); };
	const own = (runtime as any).refresh;
	registerProviderScoped(runtime, azizamProvider({ env: { AZIZAM_BASE_URL: "http://gw.test/v1" } }));
	assert.equal((runtime as any).refresh, own, "refresh must be restored after registration");
	assert.deepEqual(seen, [], "the unscoped refresh must not reach Pi during registration");
});

test("scoped registration forwards scoped refreshes and restores every descriptor on throw", async () => {
	const { registerProviderScoped } = await import("../src/provider.ts");
	const provider = {} as Parameters<typeof registerProviderScoped>[1];

	for (const own of [false, true]) {
		for (const throws of [false, true]) {
			const calls: { receiver: unknown; options: unknown }[] = [];
			const result = { aborted: false, errors: new Map() };
			const scopedOptions = { providers: ["azizam"], allowNetwork: false };
			const failure = new Error("registration failed");
			const refresh = async function (this: unknown, options?: unknown) {
				calls.push({ receiver: this, options });
				return result;
			};
			const runtime = Object.create({ refresh });
			if (own) {
				Object.defineProperty(runtime, "refresh", { value: refresh, configurable: true, enumerable: false, writable: true });
			}
			const before = Object.getOwnPropertyDescriptor(runtime, "refresh");
			let unscoped: Promise<any> | undefined;
			let scoped: Promise<any> | undefined;
			runtime.registerNativeProvider = (received: unknown) => {
				assert.equal(received, provider);
				unscoped = runtime.refresh();
				scoped = runtime.refresh(scopedOptions);
				if (throws) throw failure;
			};

			if (throws) {
				assert.throws(() => registerProviderScoped(runtime, provider), (error: unknown) => error === failure);
			} else {
				registerProviderScoped(runtime, provider);
			}

			assert.deepEqual(Object.getOwnPropertyDescriptor(runtime, "refresh"), before, "restore the original own descriptor or remove the temporary own property");
			assert.equal(runtime.refresh, refresh);
			assert.ok(unscoped);
			assert.ok(scoped);
			assert.deepEqual(await unscoped, { aborted: false, errors: new Map() });
			assert.equal(await scoped, result);
			assert.equal(calls.length, 1);
			assert.equal(calls[0].receiver, runtime);
			assert.equal(calls[0].options, scopedOptions);

			await runtime.refresh({ providers: ["after-registration"] });
			assert.equal(calls.length, 2, "normal refresh must still work afterwards");
		}
	}
});
