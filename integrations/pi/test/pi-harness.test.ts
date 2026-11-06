// The harness must not hand a dead Pi's orphaned store lock to the next Pi on the same agent dir.
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { startPi } from "./pi-harness.ts";

test("close() removes the store lock a Pi left behind when it exited mid-acquisition", async () => {
	const root = mkdtempSync(join(homedir(), ".tmp", "pi-harness-test-"));
	try {
		// Stand-in for Pi exiting between mkdir of the lock directory and its registration for exit cleanup.
		const bin = join(root, "fake-pi.mjs");
		writeFileSync(bin, [
			"#!/usr/bin/env node",
			'import { mkdirSync } from "node:fs";',
			'import { join } from "node:path";',
			'mkdirSync(join(process.env.PI_CODING_AGENT_DIR, "models-store.json.lock"));',
			'process.stdin.resume(); process.stdin.on("end", () => process.exit(0));',
		].join("\n"));
		chmodSync(bin, 0o755);
		const agentDir = join(root, "agent");
		mkdirSync(agentDir);
		const session = startPi({ agentDir, bin });
		const lock = join(agentDir, "models-store.json.lock");
		for (let i = 0; !existsSync(lock); i++) { assert.ok(i < 400, "fake Pi never took the lock"); await new Promise((r) => setTimeout(r, 25)); }
		await session.close();
		assert.equal(existsSync(lock), false, "orphaned lock survived close()");
	} finally { rmSync(root, { recursive: true, force: true }); }
});
