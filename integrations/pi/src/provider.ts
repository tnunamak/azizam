import { createProvider, envApiKeyAuth, type Model, type Models, type Provider } from "@earendil-works/pi-ai";
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

/** How long the cold-start prefetch may delay Pi startup. */
export const PREFETCH_TIMEOUT_MS = 3_000;

export interface AzizamProvider extends Provider {
	/**
	 * Cold-start discovery, called once at extension load with a model collection that already holds this provider
	 * (Pi's own `ModelRuntime`, which resolves credentials exactly as Pi does: `auth.json`, `models.json` `apiKey`
	 * including `!command`, `AZIZAM_API_KEY`). Restores the persisted catalog; if none is usable for the configured
	 * base URL, runs one bounded, provider-scoped refresh so `--provider azizam --model <id>` resolves on the very
	 * first run. Never throws. Skipped when offline.
	 */
	prefetch(models: Pick<Models, "refresh">): Promise<void>;
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
			return catalog.models.map(toModel);
		},
	});

	// Models persisted by an earlier run keep the base URL they were fetched with; always serve the current one.
	const current = () => {
		const url = baseUrl();
		return (provider.getAllModels?.() ?? provider.getModels()).map((model) => ({ ...model, baseUrl: url }));
	};

	const hasUsablePersistedCatalog = () => {
		const url = baseUrl();
		const models = provider.getAllModels?.() ?? provider.getModels();
		return models.length > 0 && models.every((model) => model.baseUrl === url);
	};

	return {
		...provider,
		getModels: current,
		getAllModels: current,
		prefetch: async (models) => {
			if (env().PI_OFFLINE || process.argv.includes("--offline")) return;
			try {
				// A restore failure (unreadable or malformed store) counts as a miss.
				await models.refresh({ allowNetwork: false, providers: [PROVIDER_ID] });
				if (hasUsablePersistedCatalog()) return;
				await models.refresh({
					allowNetwork: true,
					providers: [PROVIDER_ID],
					signal: AbortSignal.timeout(PREFETCH_TIMEOUT_MS),
				});
			} catch {
				// Pi's own refresh reports the error; the persisted catalog (if any) still applies.
			}
		},
	};
}
