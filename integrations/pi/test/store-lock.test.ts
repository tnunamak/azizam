// Pi core guards models-store.json with a proper-lockfile directory lock. An orphan left by a Pi that died between
// mkdir and registration must not stall our startup past its budget; a lock a live process refreshes must survive.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";
import { after, mock, test, type TestContext } from "node:test";
import { createRepairingModelsStore } from "../src/models-store.ts";

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

/** A child that takes the lock, optionally keeps refreshing it (stale=2 s, refresh 1 s), and reports readiness. */
function holder(path: string, mode: "refresh" | "crash") {
	const code = mode === "refresh"
		? `const l=require("proper-lockfile");l.lock(process.argv[1],{realpath:false,stale:2000,retries:0}).then(()=>{console.log("held");setInterval(()=>{},1000)})`
		: `require("node:fs").mkdirSync(process.argv[1]+".lock");console.log("held");setInterval(()=>{},1000)`;
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
	const { child, ready } = holder(path, "crash");
	await ready;
	child.kill("SIGKILL"); // dies holding an unrefreshed lock directory
	await new Promise((resolve) => child.once("exit", resolve));
	assert.ok(existsSync(lock));
	const old = new Date(Date.now() - 20_000);
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

test("a lock refreshed by a live holder is never removed; the store degrades within budget and logs the lock path", { timeout: 30_000 }, async (t) => {
	const { path, lock } = freshDir("live");
	const lockfile = createRequire(import.meta.url)("proper-lockfile");
	t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: Date.now() });
	const release = await lockfile.lock(path, { realpath: false, stale: 2000, retries: 0 });
	t.after(async () => { await release(); t.mock.timers.reset(); });
	const logged = mock.method(console, "error", () => {});
	t.after(() => logged.mock.restore());
	const beforeRefresh = statSync(lock).mtimeMs;
	t.mock.timers.tick(1500);
	for (let i = 0; i < 10 && statSync(lock).mtimeMs <= beforeRefresh; i++)
		await new Promise(setImmediate); // let proper-lockfile's asynchronous heartbeat write finish
	assert.ok(statSync(lock).mtimeMs > beforeRefresh, "live holder must refresh the lock itself");
	const controlled = controlledBudgets(t);
	const store = await createRepairingModelsStore(path, { lockBudgetMs: 1200, orphanStaleMs: 1800 });
	const read = store.read("azizam");
	await controlled.nextBudget(0);
	controlled.budgets[0].abort(new DOMException("read lock budget", "TimeoutError"));
	assert.equal(await read, undefined);
	const write = store.write("azizam", { models: [model] });
	await controlled.nextBudget(1);
	controlled.budgets[1].abort(new DOMException("write lock budget", "TimeoutError"));
	await write;
	assert.deepEqual(controlled.durations, [1200, 1200]);
	assert.ok(existsSync(lock), "live lock must survive");
	assert.equal(readFileSync(path, "utf8"), "{}", "write is skipped while the live lock is held");
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
