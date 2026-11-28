// Pi core guards models-store.json with a proper-lockfile directory lock. An orphan left by a Pi that died between
// mkdir and registration must not stall our startup past its budget; a lock a live process refreshes must survive.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";
import { after, mock, test, type TestContext } from "node:test";
import { ModelsStoreWriteTimeoutError, createRepairingModelsStore, quarantineCorruptModelsStore } from "../src/models-store.ts";

mkdirSync(join(homedir(), ".tmp"), { recursive: true });
const root = mkdtempSync(join(homedir(), ".tmp", "pi-store-lock-"));
after(() => rmSync(root, { recursive: true, force: true }));

function freshDir(name: string): { path: string; lock: string } {
	const dir = join(root, name);
	mkdirSync(dir, { recursive: true });
	const path = join(dir, "models-store.json");
	writeFileSync(path, "{}", { mode: 0o600 });
	return { path, lock: `${path}.lock` };
}

const model = {
	id: "m", name: "M", provider: "azizam", api: "openai-completions" as const, baseUrl: "http://g.test/v1", reasoning: false,
	input: ["text"] as ("text" | "image")[], contextWindow: 1, maxTokens: 1, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};

function controlledBudgets(t: TestContext) {
	const budgets: AbortController[] = [];
	const durations: number[] = [];
	let created!: () => void;
	let createdPromise = new Promise<void>((resolve) => { created = resolve; });
	t.mock.method(AbortSignal, "timeout", (ms: number) => {
		durations.push(ms);
		const controller = new AbortController();
		budgets.push(controller);
		created();
		createdPromise = new Promise<void>((resolve) => { created = resolve; });
		return controller.signal;
	});
	return {
		budgets,
		durations,
		async nextBudget(index: number) {
			while (budgets.length <= index) await createdPromise;
		},
	};
}

test("reading an empty Azizam catalog leaves unrelated stored provider data alone", async (t) => {
	const { path } = freshDir("foreign-provider");
	const original = JSON.stringify({ "another-provider": { extensionFormat: 1 } });
	writeFileSync(path, original);
	const logged = t.mock.method(console, "error", () => {});
	const store = await createRepairingModelsStore(path);
	assert.equal(await store.read("azizam"), undefined);
	assert.equal(readFileSync(path, "utf8"), original);
	assert.equal(logged.mock.calls.length, 0);
});

test("two separate repairs preserve the corrupt bodies and warn only once per store", async (t) => {
	const { path } = freshDir("repeat-corruption");
	const logged = t.mock.method(console, "error", () => {});
	const store = await createRepairingModelsStore(path);
	for (const content of ["{bad-one", "{bad-two"]) {
		writeFileSync(path, content);
		assert.equal(await store.read("azizam"), undefined);
	}
	const dir = join(root, "repeat-corruption");
	const quarantined = readdirSync(dir)
		.filter((name) => name.startsWith("models-store.json.corrupt-"))
		.map((name) => readFileSync(join(dir, name), "utf8"))
		.sort();
	assert.deepEqual(quarantined, ["{bad-one", "{bad-two"]);
	assert.equal(logged.mock.calls.filter(({ arguments: args }) => String(args[0]).includes("quarantined")).length, 1);
});

/** A child that dies after creating the lock directory, before registering it. */
function orphanHolder(path: string) {
	const code = `require("node:fs").mkdirSync(process.argv[1]+".lock");console.log("held");setInterval(()=>{},1000)`;
	const piDir = join(import.meta.dirname, "..", "node_modules");
	const child = spawn(process.execPath, ["-e", code, path], { env: { ...process.env, NODE_PATH: piDir }, stdio: ["ignore", "pipe", "inherit"] });
	const ready = new Promise<void>((resolve, reject) => {
		child.stdout.on("data", (d) => String(d).includes("held") && resolve());
		child.on("error", reject);
	});
	return { child, ready };
}

test("orphaned lock from a crashed holder is reclaimed without stalling", async () => {
	const { path, lock } = freshDir("crash");
	const { child, ready } = orphanHolder(path);
	await ready;
	child.kill("SIGKILL"); // dies holding an unrefreshed lock directory
	await new Promise((resolve) => child.once("exit", resolve));
	assert.ok(existsSync(lock));
	const old = new Date(Date.now() - 31_000);
	utimesSync(lock, old, old);
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		await Promise.race([
			(async () => {
				const store = await createRepairingModelsStore(path, { lockBudgetMs: 3000 });
				await store.write("azizam", { models: [model] });
				assert.equal((await store.read("azizam"))?.models.length, 1);
				assert.ok(!existsSync(lock), "lock released after use");
			})(),
			new Promise<never>((_resolve, reject) => {
				timer = setTimeout(() => reject(new Error("orphan reclaim stalled beyond 2.2 seconds")), 2200);
			}),
		]);
	} finally { if (timer) clearTimeout(timer); }
});


test("a 20-second-old lock from a delayed live holder is preserved within the core stale age", { timeout: 30_000 }, async (t) => {
	const { path, lock } = freshDir("delayed-live");
	mkdirSync(lock);
	const delayed = new Date(Date.now() - 20_000);
	utimesSync(lock, delayed, delayed);
	const originalMtime = statSync(lock).mtimeMs;
	const logged = mock.method(console, "error", () => {});
	const controlled = controlledBudgets(t);
	try {
		const store = await createRepairingModelsStore(path, { lockBudgetMs: 800 });
		const write = assert.rejects(store.write("azizam", { models: [model] }), (error: unknown) => {
			assert.ok(error instanceof ModelsStoreWriteTimeoutError);
			assert.match(error.message, new RegExp(lock.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), "the error names the locked path");
			assert.match(error.message, /800 ms/, "the error names the budget");
			return true;
		});
		await controlled.nextBudget(0);
		// Reclaim runs before the budget is created; the default must not have removed this lock.
		const survivedReclaim = existsSync(lock) && statSync(lock).mtimeMs === originalMtime;
		controlled.budgets[0].abort(new DOMException("delayed holder lock budget", "TimeoutError"));
		await write;
		assert.ok(survivedReclaim, "a delayed holder still owns its 20-second-old lock");
		assert.ok(existsSync(lock), "the core-valid lock survives the bounded fallback");
		assert.equal(readFileSync(path, "utf8"), "{}", "the timed-out write is not persisted");
		assert.deepEqual(controlled.durations, [800]);
	} finally { logged.mock.restore(); }
});

test("a lock refreshed by a live holder is never removed; the store degrades within budget and logs the lock path", { timeout: 30_000 }, async (t) => {
	const { path, lock } = freshDir("live");
	const require = createRequire(import.meta.url);
	const lockfile = require("proper-lockfile");
	const lockFs = require("graceful-fs") as typeof import("node:fs");
	t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: Date.now() });
	const release = await lockfile.lock(path, { realpath: false, stale: 2000, retries: 0 });
	t.after(async () => { await release(); t.mock.timers.reset(); });
	const logged = mock.method(console, "error", () => {});
	t.after(() => logged.mock.restore());
	const beforeRefresh = statSync(lock).mtimeMs;
	let heartbeatDone!: () => void;
	const heartbeat = new Promise<void>((resolve) => { heartbeatDone = resolve; });
	const realUtimes = lockFs.utimes;
	t.mock.method(lockFs, "utimes", (...args: Parameters<typeof lockFs.utimes>) => {
		const [target, atime, mtime, callback] = args;
		realUtimes(target, atime, mtime, (error) => {
			callback(error);
			if (target === lock && !error) heartbeatDone();
		});
	});
	t.mock.timers.tick(1500);
	const heartbeatWatchdog = new AbortController();
	try {
		await Promise.race([
			heartbeat,
			import("node:timers/promises").then(({ setTimeout: delay }) =>
				delay(2000, undefined, { signal: heartbeatWatchdog.signal, ref: false }).then(() => {
					throw new Error("live holder did not refresh its lock");
				})),
		]);
	} finally { heartbeatWatchdog.abort(); }
	assert.ok(statSync(lock).mtimeMs > beforeRefresh, "live holder must refresh the lock itself");
	// A live heartbeat is still valid partway to the orphan threshold.
	t.mock.timers.tick(500);
	const age = Date.now() - statSync(lock).mtimeMs;
	assert.ok(age > 1 && age < 1800, `expected a mid-age live lock, got ${age} ms`);
	const realCheck = lockfile.check.bind(lockfile);
	const orphanCheckAges: number[] = [];
	t.mock.method(lockfile, "check", (target: string, options: { stale: number; realpath: false }) => {
		orphanCheckAges.push(options.stale);
		return realCheck(target, options);
	});
	const controlled = controlledBudgets(t);
	const store = await createRepairingModelsStore(path, { lockBudgetMs: 1200, orphanStaleMs: 1800 });
	const read = store.read("azizam");
	await controlled.nextBudget(0);
	controlled.budgets[0].abort(new DOMException("read lock budget", "TimeoutError"));
	assert.equal(await read, undefined);
	const write = assert.rejects(store.write("azizam", { models: [model] }), ModelsStoreWriteTimeoutError);
	await controlled.nextBudget(1);
	controlled.budgets[1].abort(new DOMException("write lock budget", "TimeoutError"));
	await write;
	assert.deepEqual(controlled.durations, [1200, 1200]);
	assert.deepEqual(orphanCheckAges, [1800, 1800], "both operations must use the configured orphan threshold");
	assert.ok(existsSync(lock), "live lock must survive");
	assert.equal(readFileSync(path, "utf8"), "{}", "timed-out write is not persisted");
	const lines = logged.mock.calls.map((call) => String(call.arguments[0])).filter((line) => line.includes(lock));
	assert.equal(lines.length, 1, "exactly one log line naming the lock path");
	assert.match(lines[0], /continuing without the saved catalog/);
});

test("a fresh orphan (holder just died) degrades within budget instead of blocking for the 30 s stale age", { timeout: 30_000 }, async (t) => {
	const { path, lock } = freshDir("fresh-orphan");
	mkdirSync(lock);
	const logged = mock.method(console, "error", () => {});
	const controlled = controlledBudgets(t);
	try {
		const store = await createRepairingModelsStore(path, { lockBudgetMs: 800 });
		const read = store.read("azizam");
		await controlled.nextBudget(0);
		controlled.budgets[0].abort(new DOMException("fresh orphan lock budget", "TimeoutError"));
		assert.equal(await read, undefined);
		assert.deepEqual(controlled.durations, [800]);
		assert.ok(existsSync(lock));
		assert.ok(logged.mock.calls.some((call) => String(call.arguments[0]).includes(lock)));
	} finally { logged.mock.restore(); }
});

test("model-store quarantine refuses Windows before either file operation", () => {
	const calls: unknown[][] = [];
	const files = {
		chmodSync: (...args: unknown[]) => { calls.push(args); },
		renameSync: (...args: unknown[]) => { calls.push(args); },
	};
	assert.throws(
		() => quarantineCorruptModelsStore("synthetic-source", "synthetic-backup", "win32", files),
		/original was left unchanged/,
	);
	assert.deepEqual(calls, []);
	quarantineCorruptModelsStore("synthetic-source", "synthetic-backup", "linux", files);
	assert.deepEqual(calls, [
		["synthetic-source", 0o600],
		["synthetic-source", "synthetic-backup"],
	]);
});
