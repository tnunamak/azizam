// Opt-in live smoke test against a real gateway: runs only with AZIZAM_LIVE_TEST=1, never contacts anything otherwise.
// Skips cleanly when the gateway is unreachable or no key is found.
// Key source: $AZIZAM_API_KEY, else the file named by `!cat <file>` in ~/.config/pi-t3/models.json (providers["vivid-fish"].apiKey).
// The key is never printed.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fetchCatalog } from "../src/gateway.ts";

const ENABLED = process.env.AZIZAM_LIVE_TEST === "1";
const BASE_URL = process.env.AZIZAM_LIVE_BASE_URL ?? "http://127.0.0.1:5000/v1";

function readKey(): string | undefined {
	if (process.env.AZIZAM_API_KEY) return process.env.AZIZAM_API_KEY;
	try {
		const config = JSON.parse(readFileSync(join(homedir(), ".config/pi-t3/models.json"), "utf8"));
		const command: string = config.providers?.["vivid-fish"]?.apiKey ?? "";
		const match = /^!cat\s+(\S+)$/.exec(command);
		return match ? readFileSync(match[1].replace(/^~/, homedir()), "utf8").trim() : undefined;
	} catch {
		return undefined;
	}
}

test("live gateway: catalog lists chat models via the contract or the documented fallback", { skip: ENABLED ? false : "set AZIZAM_LIVE_TEST=1 to run" }, async (t) => {
	const apiKey = readKey();
	if (!apiKey) return t.skip("no API key available");
	let catalog;
	try {
		catalog = await fetchCatalog({ baseUrl: BASE_URL, apiKey, timeoutMs: 5000 });
	} catch (error) {
		if (error instanceof Error && /Could not reach|Timed out/.test(error.message)) return t.skip(`gateway unreachable: ${BASE_URL}`);
		throw error;
	}
	t.diagnostic(`filter mode: ${catalog.mode}; ${catalog.models.length} chat models; malformed: ${catalog.malformed}`);
	assert.ok(catalog.models.length > 0, "expected at least one chat model");
	for (const model of catalog.models) {
		assert.ok(model.id && model.contextWindow > 0 && model.maxTokens > 0 && model.input.includes("text"));
	}
});
