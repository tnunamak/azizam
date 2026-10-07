import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { startMockGateway } from "./mock-gateway.ts";
import { findPi, startPi } from "./pi-harness.ts";

let piAvailable = true;
try { execFileSync(findPi()!, ["--version"], { stdio: "pipe" }); } catch { piAvailable = false; }

const verifyRepair = async (broken: string) => {
	const key = "test-store-repair-key";
	const gateway = await startMockGateway({ apiKey: key });
	const agentDir = mkdtempSync(join(homedir(), ".tmp", "pi-store-repair-"));
	const path = join(agentDir, "models-store.json");
	writeFileSync(path, broken, { mode: 0o644 });
	const env = { AZIZAM_BASE_URL: gateway.baseUrl, AZIZAM_API_KEY: key };
	try {
		const online = startPi({ agentDir, env, args: ["--provider", "azizam", "--model", "coder"] });
		try {
			const state = await online.send({ type: "get_state" });
			assert.equal(state.data.model?.id, "coder", online.stderr());
			const deadline = Date.now() + 2500;
			while (!readdirSync(agentDir).some((name) => name.startsWith("models-store.json.corrupt-")) && Date.now() < deadline) {
				await new Promise((resolve) => setTimeout(resolve, 25));
			}
			const backups = readdirSync(agentDir).filter((name) => name.startsWith("models-store.json.corrupt-"));
			assert.equal(backups.length, 1, "corrupt store must be quarantined rather than blocking every future save");
			assert.match(backups[0], /^models-store\.json\.corrupt-\d{4}-\d{2}-\d{2}T/);
			const backup = join(agentDir, backups[0]);
			assert.equal(readFileSync(backup, "utf8"), broken);
			assert.equal(statSync(backup).mode & 0o777, 0o600);
			const stored = JSON.parse(readFileSync(path, "utf8"));
			assert.deepEqual(stored.azizam.models.map((model: any) => model.id).sort(), ["chat-small", "coder"]);
			assert.equal(statSync(path).mode & 0o777, 0o600);
			assert.equal((online.stderr().match(/\[azizam\].*quarantin/gi) ?? []).length, 1);
			assert.ok(!online.stderr().includes(broken));
			assert.ok(!online.stderr().includes(key));
		} finally { await online.close(); }

		const requests = gateway.requests.length;
		const offline = startPi({ agentDir, env: { ...env, PI_OFFLINE: "1" }, args: ["--provider", "azizam", "--model", "coder"] });
		try {
			const state = await offline.send({ type: "get_state" });
			assert.equal(state.data.model?.id, "coder", offline.stderr());
			const listed = await offline.send({ type: "get_available_models" });
			assert.deepEqual(listed.data.models.filter((model: any) => model.provider === "azizam").map((model: any) => model.id).sort(), ["chat-small", "coder"]);
			assert.equal(gateway.requests.length, requests);
			assert.ok(!offline.stderr().includes("quarantin"));
		} finally { await offline.close(); }
	} finally {
		await gateway.close();
		rmSync(agentDir, { recursive: true, force: true });
	}
};

for (const [label, broken] of [
	["invalid JSON", '{"azizam": broken catalog'],
	["missing models array", '{"azizam":{}}'],
	["null model", '{"azizam":{"models":[null]}}'],
]) {
	test(`${label} catalog is quarantined once, saved after discovery, and restored offline`, { skip: !piAvailable }, async () => {
		await verifyRepair(broken);
	});
}
