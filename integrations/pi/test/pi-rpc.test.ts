// End-to-end: the real `pi` CLI loads the extension from source and talks to a mock Azizam gateway over RPC.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { after, before, test } from "node:test";
import { type MockGateway, startMockGateway } from "./mock-gateway.ts";
import { findPi, startPi } from "./pi-harness.ts";

const KEY = "test-key-not-a-secret-9f3";
let piAvailable = true;
try { execFileSync(findPi()!, ["--version"], { stdio: "pipe" }); } catch { piAvailable = false; }

let gateway: MockGateway;
const dirs: string[] = [];
const freshAgentDir = () => {
	// Disk-backed ~/.tmp, not RAM-backed /tmp.
	const dir = mkdtempSync(join(homedir(), ".tmp", "pi-azizam-test-"));
	dirs.push(dir);
	return dir;
};
/** Azizam models as Pi reports them. Pi's refresh runs in the background, so wait (briefly) until some appear. */
const azizamModels = async (session: ReturnType<typeof startPi>, until: (models: any[]) => boolean = (m) => m.length > 0, timeoutMs = 10000) => {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const response = await session.send({ type: "get_available_models" });
		assert.equal(response.success, true, JSON.stringify(response));
		const models = (response.data.models as any[]).filter((m) => m.provider === "azizam");
		if (until(models) || Date.now() > deadline) return models;
		await new Promise((resolve) => setTimeout(resolve, 100));
	}
};

/** Wait until Pi has written the azizam catalog to models-store.json (persistence is asynchronous). */
const waitForStoredCatalog = async (agentDir: string, count: number, timeoutMs = 10000) => {
	const path = join(agentDir, "models-store.json");
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		try {
			const stored = JSON.parse(readFileSync(path, "utf8"))?.azizam?.models;
			if (Array.isArray(stored) && stored.length === count) return;
		} catch { /* not written yet */ }
		assert.ok(Date.now() < deadline, `catalog not persisted to ${path}`);
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
};

/**
 * Wait until Pi's background refresh has hit the gateway (past request index `from`) and got `status`, then give Pi
 * a moment to record the outcome. Callers keep the forced failure active until their catalog check is done.
 */
const waitForRefreshFailure = async (mock: MockGateway, from: number, status: number, timeoutMs = 10000) => {
	const deadline = Date.now() + timeoutMs;
	while (!mock.requests.slice(from).some((r) => r.url === "/v1/models" && r.status === status)) {
		assert.ok(Date.now() < deadline, `no /v1/models request answered with ${status}`);
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	await new Promise((resolve) => setTimeout(resolve, 300));
};

before(async () => { gateway = await startMockGateway({ apiKey: KEY }); });
after(async () => {
	await gateway.close();
	for (const dir of dirs) rmSync(dir, { recursive: true, force: true });
});

test("lists exactly the language models with mapped metadata", { skip: !piAvailable }, async () => {
	const session = startPi({ agentDir: freshAgentDir(), env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY } });
	try {
		const models = await azizamModels(session);
		assert.deepEqual(models.map((m) => m.id).sort(), ["chat-small", "coder"]);
		const coder = models.find((m) => m.id === "coder");
		assert.equal(coder.name, "coder");
		assert.equal(coder.api, "openai-completions");
		assert.equal(coder.baseUrl, gateway.baseUrl);
		assert.equal(coder.contextWindow, 200000);
		assert.equal(coder.maxTokens, 16000);
		assert.equal(coder.reasoning, true);
		assert.deepEqual(coder.input, ["text", "image"]);
		assert.equal(coder.compat.supportsDeveloperRole, false);
		assert.equal(coder.compat.maxTokensField, "max_tokens");
		const small = models.find((m) => m.id === "chat-small");
		assert.equal(small.reasoning, false);
		assert.deepEqual(small.input, ["text"]);
		assert.equal(small.contextWindow, 128000);
		const listRequests = gateway.requests.filter((r) => r.url === "/v1/models");
		assert.ok(listRequests.length >= 1);
		assert.ok(listRequests.every((r) => r.authorization === `Bearer ${KEY}`));
		assert.ok(!session.stderr().includes(KEY), "key leaked to stderr");
	} finally {
		await session.close();
	}
});

test("prompt streams text and reasoning end to end through Pi", { skip: !piAvailable }, async () => {
	const session = startPi({
		agentDir: freshAgentDir(),
		env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY },
		args: ["--provider", "azizam", "--model", "coder"],
	});
	try {
		await azizamModels(session);
		const started = await session.send({ type: "prompt", message: "Say hello" });
		assert.equal(started.success, true, JSON.stringify(started));
		await session.waitFor((e) => e.type === "agent_settled" || e.type === "agent_end");
		const updates = session.events.filter((e) => e.type === "message_update").map((e) => e.assistantMessageEvent);
		const text = updates.filter((u) => u.type === "text_delta").map((u) => u.delta).join("");
		const thinking = updates.filter((u) => u.type === "thinking_delta").map((u) => u.delta).join("");
		assert.equal(text, "Hello from Azizam");
		assert.equal(thinking, "thinking...");
		const chat = gateway.requests.filter((r) => r.url === "/v1/chat/completions").at(-1)!;
		assert.equal(chat.authorization, `Bearer ${KEY}`);
		const body = JSON.parse(chat.body!);
		assert.equal(body.model, "coder");
		assert.equal(body.stream, true);
		assert.ok("max_tokens" in body && !("max_completion_tokens" in body), "max_tokens field per compat");
		assert.ok(body.messages.every((m: any) => m.role !== "developer"), "no developer role per compat");
		assert.ok(!session.stderr().includes(KEY), "key leaked to stderr");
	} finally {
		await session.close();
	}
});

test("catalog persists for offline start and survives a failed refresh", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	const env = { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY };

	const first = startPi({ agentDir, env });
	let online: any[];
	try {
		online = await azizamModels(first);
		await waitForStoredCatalog(agentDir, 2);
	} finally { await first.close(); }
	assert.equal(online.length, 2);

	// Offline mode: the network is never touched, the stored catalog is restored.
	const before = gateway.requests.length;
	const offline = startPi({ agentDir, env: { ...env, PI_OFFLINE: "1" } });
	try {
		assert.deepEqual((await azizamModels(offline)).map((m) => m.id).sort(), ["chat-small", "coder"]);
		assert.equal(gateway.requests.length, before, "offline start must not call the gateway");
	} finally { await offline.close(); }

	// Online but the gateway fails (503): last good catalog stays. The failure stays forced until the refresh has
	// hit the gateway and Pi has had time to report its outcome; only then is the catalog checked.
	gateway.setStatus(503);
	const failing = startPi({ agentDir, env });
	try {
		await waitForRefreshFailure(gateway, before, 503);
		assert.deepEqual((await azizamModels(failing)).map((m) => m.id).sort(), ["chat-small", "coder"]);
		assert.ok(!failing.stderr().includes(KEY), "key leaked to stderr");
	} finally { gateway.setStatus(200); await failing.close(); }

	// A wrong key (401) also keeps the last catalog.
	const beforeWrong = gateway.requests.length;
	const wrongKey = startPi({ agentDir, env: { ...env, AZIZAM_API_KEY: "wrong-key-abc" } });
	try {
		await waitForRefreshFailure(gateway, beforeWrong, 401);
		assert.deepEqual((await azizamModels(wrongKey)).map((m) => m.id).sort(), ["chat-small", "coder"]);
		assert.ok(!wrongKey.stderr().includes("wrong-key-abc"), "key leaked to stderr");
	} finally { await wrongKey.close(); }
});

test("a refreshed catalog replaces the stored one", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	const env = { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY };
	const first = startPi({ agentDir, env });
	try {
		await azizamModels(first);
		await waitForStoredCatalog(agentDir, 2);
	} finally { await first.close(); }
	gateway.setModels({ data: [{ id: "only-one", type: "language", context_window: 4096 }] });
	const second = startPi({ agentDir, env });
	try {
		const models = await azizamModels(second, (m) => m.some((x) => x.id === "only-one"));
		assert.deepEqual(models.map((m) => m.id), ["only-one"]);
		assert.equal(models[0].contextWindow, 4096);
	} finally {
		await second.close();
		const { CONTRACT_LIST } = await import("./mock-gateway.ts");
		gateway.setModels(CONTRACT_LIST);
	}
});

test("first run with no stored catalog resolves --provider/--model; offline first run makes no request", { skip: !piAvailable }, async () => {
	const env = { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY };
	const cold = startPi({ agentDir: freshAgentDir(), env, args: ["--provider", "azizam", "--model", "chat-small"] });
	try {
		const state = await cold.send({ type: "get_state" });
		assert.equal(state.data.model?.id, "chat-small", cold.stderr());
	} finally { await cold.close(); }

	const before = gateway.requests.length;
	const offline = startPi({ agentDir: freshAgentDir(), env: { ...env, PI_OFFLINE: "1" } });
	try {
		assert.deepEqual(await azizamModels(offline, () => false, 1500), []);
		assert.equal(gateway.requests.length, before);
	} finally { await offline.close(); }
});

test("models.json modelOverrides win over the provider defaults", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(
		join(agentDir, "models.json"),
		JSON.stringify({
			providers: { azizam: { modelOverrides: { coder: { maxTokens: 4000, compat: { thinkingFormat: "openrouter" } } } } },
		}),
	);
	const session = startPi({ agentDir, env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY } });
	try {
		const models = await azizamModels(session, (m) => m.length === 2);
		const coder = models.find((m) => m.id === "coder");
		assert.equal(coder.maxTokens, 4000, JSON.stringify(coder));
		assert.equal(coder.compat.thinkingFormat, "openrouter");
		assert.equal(coder.compat.maxTokensField, "max_tokens", "unlisted compat keys keep the provider default");
		assert.equal(models.find((m) => m.id === "chat-small").maxTokens, 8192);
	} finally { await session.close(); }
});

test("works when installed as a package (settings.json), not just via --extension", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(join(agentDir, "settings.json"), JSON.stringify({ packages: [resolve(import.meta.dirname, "..")] }));
	const session = startPi({ agentDir, viaPackage: true, env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY } });
	try {
		assert.deepEqual((await azizamModels(session)).map((m) => m.id).sort(), ["chat-small", "coder"]);
	} finally { await session.close(); }
});

// Cold start with the key supplied only through Pi (no AZIZAM_API_KEY): T3 Code launches
// `pi --mode rpc --provider azizam --model X` on a fresh agent dir exactly like this.
const coldStartResolves = async (agentDir: string, label: string) => {
	assert.equal(existsSync(join(agentDir, "models-store.json")), false);
	const session = startPi({
		agentDir,
		env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: "" },
		args: ["--provider", "azizam", "--model", "chat-small"],
	});
	try {
		const state = await session.send({ type: "get_state" });
		assert.equal(state.data.model?.id, "chat-small", `${label}: ${session.stderr()}`);
		assert.ok(!session.stderr().includes(KEY), "key leaked to stderr");
		assert.ok(gateway.requests.some((r) => r.url === "/v1/models" && r.authorization === `Bearer ${KEY}`));
	} finally { await session.close(); }
};

test("cold start resolves --provider/--model with the key in auth.json only", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(join(agentDir, "auth.json"), JSON.stringify({ azizam: { type: "api_key", key: KEY } }));
	await coldStartResolves(agentDir, "auth.json");
});

test("cold start resolves --provider/--model with a models.json apiKey (literal and !command)", { skip: !piAvailable }, async () => {
	for (const apiKey of [KEY, `!echo ${KEY}`]) {
		const agentDir = freshAgentDir();
		writeFileSync(join(agentDir, "models.json"), JSON.stringify({ providers: { azizam: { apiKey } } }));
		await coldStartResolves(agentDir, apiKey.startsWith("!") ? "!command" : "literal");
	}
});

test("cold start resolves even when the model store cannot be read", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(join(agentDir, "auth.json"), JSON.stringify({ azizam: { type: "api_key", key: KEY } }));
	mkdirSync(join(agentDir, "models-store.json")); // reading it fails with EISDIR
	const session = startPi({
		agentDir,
		env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: "" },
		args: ["--provider", "azizam", "--model", "chat-small"],
	});
	try {
		const state = await session.send({ type: "get_state" });
		assert.equal(state.data.model?.id, "chat-small", session.stderr());
	} finally { await session.close(); }
});

// Supported claim: model discovery only. Pi reads auth.json again when it lists and authenticates, so a read-only
// agent dir cannot prompt; the README requires a usable credential store for that. Asserting more would overclaim.
test("cold start discovers the model when the agent dir is read-only (discovery only, not prompting)", { skip: !piAvailable || process.getuid?.() === 0 }, async () => {
	const agentDir = freshAgentDir();
	chmodSync(agentDir, 0o555);
	const session = startPi({
		agentDir,
		env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: KEY },
		args: ["--provider", "azizam", "--model", "chat-small"],
	});
	try {
		const state = await session.send({ type: "get_state" });
		assert.equal(state.data.model?.id, "chat-small", session.stderr());
	} finally { await session.close(); chmodSync(agentDir, 0o755); }
});

// The normal writable case, end to end: cold start with the key only in auth.json, then list and prompt.
test("cold start with a writable auth.json lists the models and completes a prompt", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(join(agentDir, "auth.json"), JSON.stringify({ azizam: { type: "api_key", key: KEY } }));
	const session = startPi({
		agentDir,
		env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: "" },
		args: ["--provider", "azizam", "--model", "coder"],
	});
	try {
		const listed = await azizamModels(session, (m) => m.length === 2);
		assert.deepEqual(listed.map((m) => m.id).sort(), ["chat-small", "coder"]);
		const started = await session.send({ type: "prompt", message: "Say hello" });
		assert.equal(started.success, true, JSON.stringify(started));
		await session.waitFor((e) => e.type === "agent_settled" || e.type === "agent_end");
		const text = session.events
			.filter((e) => e.type === "message_update").map((e) => e.assistantMessageEvent)
			.filter((u) => u.type === "text_delta").map((u) => u.delta).join("");
		assert.equal(text, "Hello from Azizam", session.stderr());
		const chat = gateway.requests.filter((r) => r.url === "/v1/chat/completions").at(-1)!;
		assert.equal(chat.authorization, `Bearer ${KEY}`);
		assert.ok(!session.stderr().includes(KEY), "key leaked to stderr");
	} finally { await session.close(); }
});

// Credential commands of unrelated providers (password managers) must not run because this extension is loaded.
// End-to-end guard only: Pi caches `auth.json` command results per process, so this passes even without the scoped
// registration. The test that proves the isolation is test/runtime-isolation.test.ts.
test("loading the extension adds no credential command runs for other providers", { skip: !piAvailable }, async () => {
	const countRuns = async (withExtension: boolean) => {
		const agentDir = freshAgentDir();
		const counter = join(agentDir, "other.count");
		writeFileSync(join(agentDir, "auth.json"), JSON.stringify({
			openai: { type: "api_key", key: `!echo run >> ${counter}; echo k-other` },
			azizam: { type: "api_key", key: KEY },
		}));
		const session = startPi({
			agentDir,
			noExtension: !withExtension,
			env: { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: "" },
			args: withExtension ? ["--provider", "azizam", "--model", "chat-small"] : [],
		});
		try {
			await session.send({ type: "get_state" });
			await session.send({ type: "get_available_models" });
			await new Promise((resolve) => setTimeout(resolve, 1000));
		} finally { await session.close(); }
		return existsSync(counter) ? readFileSync(counter, "utf8").split("\n").filter(Boolean).length : 0;
	};
	const baseline = await countRuns(false);
	assert.equal(await countRuns(true), baseline, "the extension must not trigger extra runs of other providers' commands");
});

test("a cold-start models.json !command key runs at most twice (prefetch + Pi's own refresh), not more", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	const counter = join(agentDir, "azizam.count");
	writeFileSync(join(agentDir, "models.json"), JSON.stringify({ providers: { azizam: { apiKey: `!echo run >> ${counter}; echo ${KEY}` } } }));
	await coldStartResolves(agentDir, "counted !command");
	await new Promise((resolve) => setTimeout(resolve, 500));
	const runs = readFileSync(counter, "utf8").split("\n").filter(Boolean).length;
	assert.ok(runs >= 1 && runs <= 2, `ran ${runs} times`);
});

test("models.json baseUrl drives discovery and chat; conflicting env URL sends no credentials", { skip: !piAvailable }, async () => {
	const agentDir = freshAgentDir();
	writeFileSync(join(agentDir, "models.json"), JSON.stringify({ providers: { azizam: { baseUrl: gateway.baseUrl, apiKey: KEY } } }));
	const session = startPi({ agentDir, env: { AZIZAM_BASE_URL: "", AZIZAM_API_KEY: "" }, args: ["--provider", "azizam", "--model", "coder"] });
	try {
		const state = await session.send({ type: "get_state" });
		assert.equal(state.data.model?.baseUrl, gateway.baseUrl, session.stderr());
		const started = await session.send({ type: "prompt", message: "Say hello" });
		assert.equal(started.success, true);
		await session.waitFor((e) => e.type === "agent_settled" || e.type === "agent_end");
		assert.ok(gateway.requests.some((r) => r.url === "/v1/chat/completions"));
	} finally { await session.close(); }

	const decoy = await startMockGateway();
	const before = gateway.requests.length;
	const conflictDir = freshAgentDir();
	writeFileSync(join(conflictDir, "models.json"), JSON.stringify({ providers: { azizam: { baseUrl: gateway.baseUrl, apiKey: KEY } } }));
	const blocked = startPi({ agentDir: conflictDir, env: { AZIZAM_BASE_URL: decoy.baseUrl, AZIZAM_API_KEY: "" } });
	try {
		await blocked.send({ type: "get_state" });
		await new Promise((r) => setTimeout(r, 300));
		assert.equal(decoy.requests.length, 0);
		assert.equal(gateway.requests.length, before);
		assert.match(blocked.stderr(), /AZIZAM_BASE_URL.*models.json.*conflict/);
		assert.ok(!blocked.stderr().includes(KEY));
	} finally { await blocked.close(); await decoy.close(); }
});
