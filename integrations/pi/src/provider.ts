import { createProvider, envApiKeyAuth, type Model, type Provider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import type { Catalog, ChatModelInfo } from "./catalog.ts";
import { DEFAULT_TIMEOUT_MS, fetchCatalog, normalizeBaseUrl } from "./gateway.ts";

export const PROVIDER_ID = "azizam";

/** Verified against the gateway; override per model through models.json `modelOverrides`. */
export const DEFAULT_COMPAT = {
	supportsDeveloperRole: false,
	supportsReasoningEffort: true,
	supportsStrictMode: false,
	maxTokensField: "max_tokens",
	thinkingFormat: "openai",
} as const;

export interface AzizamProviderOptions {
	/** Environment source; defaults to process.env. Read on every refresh, so changes apply on the next refresh. */
	env?: Record<string, string | undefined>;
	fetch?: typeof fetch;
}

/** How long the cold-start prefetch may delay Pi startup. */
export const PREFETCH_TIMEOUT_MS = 3_000;

export interface AzizamProvider extends Provider {
	/**
	 * Best-effort fetch used once at extension load so the very first run (no persisted catalog yet) already has models
	 * for `--provider azizam --model <id>` and the first RPC `get_available_models`. Never throws. Needs AZIZAM_API_KEY
	 * in the environment (stored `/login` and models.json keys are only visible to Pi's own refresh), skipped when offline.
	 */
	prefetch(): Promise<void>;
}

export function azizamProvider(options: AzizamProviderOptions = {}): AzizamProvider {
	const env = () => options.env ?? process.env;
	const baseUrl = () => normalizeBaseUrl(env().AZIZAM_BASE_URL);

	const toModel = (info: ChatModelInfo): Model<"openai-completions"> => ({
		...info,
		api: "openai-completions",
		provider: PROVIDER_ID,
		baseUrl: baseUrl(),
		compat: { ...DEFAULT_COMPAT },
	});

	const toModels = (catalog: Catalog) => catalog.models.map(toModel);
	let seed: Model<"openai-completions">[] = [];

	const provider = createProvider<"openai-completions">({
		id: PROVIDER_ID,
		name: "Azizam",
		auth: { apiKey: envApiKeyAuth("Azizam API key", ["AZIZAM_API_KEY"]) },
		models: [],
		api: openAICompletionsApi(),
		fetchModels: async (context) => {
			const catalog: Catalog = await fetchCatalog({
				baseUrl: baseUrl(),
				apiKey: context.credential?.type === "api_key" ? context.credential.key : undefined,
				signal: context.signal,
				timeoutMs: Number(env().AZIZAM_MODELS_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS,
				fetch: options.fetch,
			});
			if (env().AZIZAM_DEBUG) {
				console.error(`[azizam] ${catalog.models.length} chat models (filter: ${catalog.mode}, skipped malformed: ${catalog.malformed})`);
			}
			return toModels(catalog);
		},
	});

	// Models persisted by an earlier run keep the base URL they were fetched with; always serve the current one.
	const rebase = <T extends { baseUrl: string }>(models: readonly T[]): T[] => {
		const current = baseUrl();
		return models.map((model) => ({ ...model, baseUrl: current }));
	};
	const current = () => {
		const models = provider.getAllModels?.() ?? provider.getModels();
		return rebase(models.length > 0 ? models : seed);
	};
	return {
		...provider,
		getModels: current,
		getAllModels: current,
		prefetch: async () => {
			const apiKey = env().AZIZAM_API_KEY;
			if (!apiKey || env().PI_OFFLINE || process.argv.includes("--offline")) return;
			try {
				seed = toModels(
					await fetchCatalog({ baseUrl: baseUrl(), apiKey, timeoutMs: PREFETCH_TIMEOUT_MS, fetch: options.fetch }),
				);
			} catch {
				// Pi's own refresh reports the error; the persisted catalog (if any) still applies.
			}
		},
	};
}
