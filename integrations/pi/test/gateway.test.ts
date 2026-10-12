import assert from "node:assert/strict";
import { test } from "node:test";
import { AzizamError, DEFAULT_BASE_URL, fetchCatalog, normalizeBaseUrl } from "../src/gateway.ts";
import { startMockGateway } from "./mock-gateway.ts";

const SECRET = "sk-super-secret-key-123";

test("normalizeBaseUrl: default, trailing slashes, rejects bad input", () => {
	assert.equal(normalizeBaseUrl(undefined), DEFAULT_BASE_URL);
	assert.equal(normalizeBaseUrl("  "), DEFAULT_BASE_URL);
	assert.equal(normalizeBaseUrl("https://gw.example.com/v1//"), "https://gw.example.com/v1");
	assert.throws(() => normalizeBaseUrl("not a url"), /not a valid URL/);
	assert.throws(() => normalizeBaseUrl("ftp://x/v1"), /http/);
	assert.throws(() => normalizeBaseUrl("http://user:pw@host/v1"), /credentials/);
});

test("sends Bearer auth and returns the mapped catalog", async () => {
	const gw = await startMockGateway({ apiKey: SECRET });
	try {
		const catalog = await fetchCatalog({ baseUrl: gw.baseUrl, apiKey: SECRET });
		assert.deepEqual(catalog.models.map((m) => m.id), ["coder", "chat-small"]);
		assert.equal(gw.requests[0].url, "/v1/models");
		assert.equal(gw.requests[0].authorization, `Bearer ${SECRET}`);
	} finally {
		await gw.close();
	}
});

test("401 and 5xx and bad bodies give actionable errors that never contain the key", async () => {
	const gw = await startMockGateway({ apiKey: SECRET });
	try {
		const failures: string[] = [];
		const attempt = async (opts: Parameters<typeof fetchCatalog>[0]) => {
			try {
				await fetchCatalog(opts);
				assert.fail("expected failure");
			} catch (error) {
				assert.ok(error instanceof AzizamError, String(error));
				failures.push(error.message);
				return error.message;
			}
		};
		assert.match(await attempt({ baseUrl: gw.baseUrl, apiKey: "wrong-key-xyz" }), /rejected the API key \(HTTP 401\)/);
		assert.match(await attempt({ baseUrl: gw.url, apiKey: SECRET }), /404.*include the API prefix/);
		gw.setStatus(503);
		assert.match(await attempt({ baseUrl: gw.baseUrl, apiKey: SECRET }), /HTTP 503/);
		gw.setStatus(200);
		gw.setModels({ nope: true });
		assert.match(await attempt({ baseUrl: gw.baseUrl, apiKey: SECRET }), /no `data` array/);
		assert.match(
			await attempt({ baseUrl: gw.baseUrl, apiKey: SECRET, fetch: async () => new Response("<html>", { status: 200 }) }),
			/did not return JSON/,
		);
		for (const message of failures) assert.ok(!message.includes(SECRET) && !message.includes("wrong-key-xyz"), message);
	} finally {
		await gw.close();
	}
});

test("gateway down: clear message, no key; timeout: clear message", async () => {
	const gw = await startMockGateway();
	const baseUrl = gw.baseUrl;
	await gw.close();
	await assert.rejects(fetchCatalog({ baseUrl, apiKey: SECRET }), (error: Error) => {
		assert.match(error.message, /Could not reach the Azizam gateway/);
		assert.ok(!error.message.includes(SECRET));
		return true;
	});
	const hang: typeof fetch = (_url, init) =>
		new Promise((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "TimeoutError"))));
	await assert.rejects(fetchCatalog({ baseUrl, fetch: hang, timeoutMs: 20 }), /Timed out after 20 ms/);
});

test("transport errors expose only allowlisted cause categories", async () => {
	const cases: [string, string][] = [
		["ETIMEDOUT", "timed out"], ["ECONNREFUSED", "connection was refused"], ["ENOTFOUND", "DNS lookup failed"],
		["CERT_HAS_EXPIRED", "TLS certificate"], ["EOTHER", "network error"], ["__proto__", "network error"], ["toString", "network error"],
	];
	for (const [code, expected] of cases) {
		const fetcher: typeof fetch = async () => { throw Object.assign(new Error("secret arbitrary transport detail"), { cause: { code, message: "secret arbitrary transport detail" } }); };
		await assert.rejects(fetchCatalog({ baseUrl: "http://gateway/v1", fetch: fetcher }), (error: Error) => {
			assert.match(error.message, new RegExp(expected));
			assert.ok(!error.message.includes("secret arbitrary transport detail"));
			return true;
		});
	}
});

test("fetch rejects same-origin redirects without requesting the target", async () => {
	const gw = await startMockGateway();
	try {
		gw.setRedirect(true);
		await assert.rejects(fetchCatalog({ baseUrl: gw.baseUrl }));
		assert.deepEqual(gw.requests.map((request) => request.url), ["/v1/models"]);
	} finally {
		await gw.close();
	}
});

test("partial response body timeout is reported as timeout", async () => {
	const gw = await startMockGateway();
	try {
		gw.setPartialBody(true);
		await assert.rejects(fetchCatalog({ baseUrl: gw.baseUrl, timeoutMs: 30 }), /Timed out after 30 ms/);
		assert.deepEqual(gw.requests.map((request) => request.url), ["/v1/models"]);
	} finally {
		await gw.close();
	}
});

test("caller abort reason is preserved by identity during fetch and body consumption", async () => {
	for (const phase of ["fetch", "body"] as const) {
		const controller = new AbortController();
		const reason = { requestedBy: "caller", phase };
		let entered!: () => void;
		const reached = new Promise<void>((resolve) => { entered = resolve; });
		const blocked = (signal: AbortSignal) => new Promise<never>((_resolve, reject) => {
			const abort = () => reject(new Error("synthetic adapter abort"));
			if (signal.aborted) abort();
			else signal.addEventListener("abort", abort, { once: true });
			entered();
		});
		const fetcher: typeof fetch = async (_url, init) => {
			assert.ok(init?.signal);
			const signal = init.signal;
			if (phase === "fetch") return blocked(signal);
			return { ok: true, status: 200, json: () => blocked(signal) } as unknown as Response;
		};

		const pending = fetchCatalog({ baseUrl: "http://gateway.test/v1", signal: controller.signal, fetch: fetcher });
		await reached;
		controller.abort(reason);
		await assert.rejects(pending, (error: unknown) => {
			assert.equal(error, reason);
			return true;
		});
	}
});

test("normalizeBaseUrl never echoes secrets and rejects userinfo, query and fragment", () => {
	for (const bad of ["http://user:topsecret@", "http://u:topsecret@h/v1", "http://h/v1?api_key=topsecret", "http://h/v1#topsecret", "http://h/v1?"]) {
		assert.throws(
			() => normalizeBaseUrl(bad),
			(error: Error) => /AZIZAM_BASE_URL/.test(error.message) && !error.message.includes("topsecret"),
			bad,
		);
	}
	assert.equal(normalizeBaseUrl("http://h:8080/v1///"), "http://h:8080/v1");
});
