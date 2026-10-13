// Pi core guards models-store.json with a proper-lockfile directory lock. An orphan left by a Pi that died between
// mkdir and registration must not stall our startup past its budget; a lock a live process refreshes must survive.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { after, mock, test } from "node:test";
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

test("orphaned lock from a crashed holder is reclaimed and the start completes within budget", async (t) => {
	const { path, lock } = freshDir("crash");
	const { child, ready } = holder(path, "crash");
	await ready;
	child.kill("SIGKILL"); // dies holding an unrefreshed lock directory
	await new Promise((resolve) => child.once("exit", resolve));
	assert.ok(existsSync(lock));
	const old = new Date(Date.now() - 20_000);
	utimesSync(lock, old, old);
	const store = await createRepairingModelsStore(path, { lockBudgetMs: 3000 });
	const started = Date.now();
	await store.write("azizam", { models: [model] });
	assert.equal((await store.read("azizam"))?.models.length, 1);
	assert.ok(Date.now() - started < 2000, "orphan must not stall the store");
	assert.ok(!existsSync(lock), "lock released after use");
	t.diagnostic(`${Date.now() - started} ms`);
});

test("a lock refreshed by a live holder is never removed; the store degrades within budget and logs the lock path", async (t) => {
	const { path, lock } = freshDir("live");
	const { child, ready } = holder(path, "refresh");
	t.after(() => child.kill("SIGKILL"));
	await ready;
	const logged = mock.method(console, "error", () => {});
	t.after(() => logged.mock.restore());
	// Outlast several refresh cycles: the lock is older than orphanStaleMs only if it were never refreshed.
	await new Promise((resolve) => setTimeout(resolve, 2500));
	const store = await createRepairingModelsStore(path, { lockBudgetMs: 1200, orphanStaleMs: 1800 });
	const started = Date.now();
	assert.equal(await store.read("azizam"), undefined);
	await store.write("azizam", { models: [model] });
	const elapsed = Date.now() - started;
	assert.ok(existsSync(lock), "live lock must survive");
	assert.ok(elapsed < 1200 * 2 + 800, `degraded reads must respect the budget, took ${elapsed} ms`);
	const lines = logged.mock.calls.map((call) => String(call.arguments[0])).filter((line) => line.includes(lock));
	assert.equal(lines.length, 1, "exactly one log line naming the lock path");
	assert.match(lines[0], /continuing without the saved catalog/);
});

test("a fresh orphan (holder just died) degrades within budget instead of blocking for the 30 s stale age", async () => {
	const { path, lock } = freshDir("fresh-orphan");
	mkdirSync(lock);
	const logged = mock.method(console, "error", () => {});
	try {
		const store = await createRepairingModelsStore(path, { lockBudgetMs: 800 });
		const started = Date.now();
		assert.equal(await store.read("azizam"), undefined);
		assert.ok(Date.now() - started < 3000);
		assert.ok(existsSync(lock));
		assert.ok(logged.mock.calls.some((call) => String(call.arguments[0]).includes(lock)));
	} finally { logged.mock.restore(); }
});
