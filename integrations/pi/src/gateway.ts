// Fetches and validates the Azizam model list. No Pi imports.
import { type Catalog, parseModelList } from "./catalog.ts";

export const DEFAULT_BASE_URL = "http://127.0.0.1:5000/v1";
export const DEFAULT_TIMEOUT_MS = 10_000;

export class AzizamError extends Error {
	override name = "AzizamError";
}

/**
 * Validate and normalise a base URL (no trailing slash). The raw input is never echoed, because it may carry
 * credentials: userinfo, query strings and fragments are rejected with a message that does not repeat them.
 */
export function normalizeBaseUrl(value: string | undefined): string {
	const raw = (value ?? "").trim() || DEFAULT_BASE_URL;
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		throw new AzizamError(`AZIZAM_BASE_URL is not a valid URL. Expected something like ${DEFAULT_BASE_URL}`);
	}
	if (url.protocol !== "http:" && url.protocol !== "https:") {
		throw new AzizamError("AZIZAM_BASE_URL must start with http:// or https://.");
	}
	if (url.username || url.password || /^[a-z][a-z0-9+.-]*:\/\/[^/?#]*@/iu.test(raw)) {
		throw new AzizamError("AZIZAM_BASE_URL must not contain credentials (user:password@); use AZIZAM_API_KEY instead.");
	}
	if (raw.includes("?") || raw.includes("#")) {
		throw new AzizamError(
			"AZIZAM_BASE_URL must not contain a query string or fragment; pass the key with AZIZAM_API_KEY instead.",
		);
	}
	return raw.replace(/\/+$/u, "");
}

export interface FetchCatalogOptions {
	baseUrl: string;
	apiKey?: string;
	signal?: AbortSignal;
	timeoutMs?: number;
	fetch?: typeof fetch;
}

/** GET {base}/models. Throws AzizamError with an actionable message; messages never contain the API key. */
export async function fetchCatalog(options: FetchCatalogOptions): Promise<Catalog> {
	const { baseUrl, apiKey, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
	const doFetch = options.fetch ?? fetch;
	const url = `${baseUrl}/models`;
	const timeout = AbortSignal.timeout(timeoutMs);
	const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
	const headers: Record<string, string> = { accept: "application/json" };
	if (apiKey) headers.authorization = `Bearer ${apiKey}`;

	let response: Response;
	try {
		response = await doFetch(url, { headers, signal: combined });
	} catch (error) {
		if (signal?.aborted) throw error;
		if (timeout.aborted) {
			throw new AzizamError(`Timed out after ${timeoutMs} ms fetching ${url}. Is the Azizam gateway reachable?`);
		}
		const cause = error instanceof Error && error.cause instanceof Error ? ` (${error.cause.message})` : "";
		throw new AzizamError(
			`Could not reach the Azizam gateway at ${baseUrl}${cause}. Check AZIZAM_BASE_URL and that the gateway is running.`,
		);
	}

	if (response.status === 401 || response.status === 403) {
		throw new AzizamError(
			`Azizam gateway rejected the API key (HTTP ${response.status}). Set AZIZAM_API_KEY or run /login azizam.`,
		);
	}
	if (response.status === 404) {
		throw new AzizamError(`${url} returned 404. AZIZAM_BASE_URL should include the API prefix, e.g. ${DEFAULT_BASE_URL}`);
	}
	if (!response.ok) {
		throw new AzizamError(`Azizam gateway returned HTTP ${response.status} for ${url}.`);
	}

	let body: unknown;
	try {
		body = await response.json();
	} catch {
		throw new AzizamError(`${url} did not return JSON. AZIZAM_BASE_URL may point at a web page instead of the API.`);
	}
	try {
		return parseModelList(body);
	} catch (error) {
		throw new AzizamError(`${url}: ${error instanceof Error ? error.message : String(error)}`);
	}
}
