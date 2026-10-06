import {
	createProvider,
	envApiKeyAuth,
	type Model,
	type Models,
	type ModelsRefreshOptions,
	type ModelsRefreshResult,
	type Provider,
} from "@earendil-works/pi-ai";
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

/**
 * How long the cold-start prefetch may delay Pi startup, as one budget shared by the stored-catalog restore, the
 * credential lookup and the network fetch. A spent budget skips the remaining steps. One step cannot be bounded: Pi
 * resolves a `models.json` `!command` key with a blocking `spawnSync` (10 s cap) that no signal can interrupt.
 */
export const PREFETCH_TIMEOUT_MS = 3_000;

export interface AzizamProviderOptions {
	/** Environment to read `AZIZAM_*` settings from. Defaults to `process.env`. */
	env?: Record<string, string | undefined>;
	/** `fetch` implementation for gateway calls. Defaults to the global one. */
	fetch?: typeof fetch;
	/** Cold-start budget in ms. Defaults to {@link PREFETCH_TIMEOUT_MS}. */
	prefetchTimeoutMs?: number;
}

/** The slice of Pi's `ModelRuntime` (or pi-ai `Models`) that the cold-start prefetch uses. */
export type PrefetchModels = Pick<Models, "refresh" | "getAuth">;

export interface AzizamProvider extends Provider {
	/**
	 * Cold-start discovery, called once at extension load with a model collection that already holds this provider
	 * (Pi's own `ModelRuntime`, which resolves credentials exactly as Pi does: `auth.json`, `models.json` `apiKey`
	 * including `!command`, `AZIZAM_API_KEY`). Restores the persisted catalog; if none is usable for the configured
	 * base URL, resolves this provider's credential and fetches once, so `--provider azizam --model <id>` resolves on
	 * the very first run. A failed restore is a cache miss, and the fetched models are served from memory even when
	 * Pi cannot persist them. Bounded by one shared budget; never throws. Skipped when offline.
	 */
	prefetch(models: PrefetchModels): Promise<void>;
}

/** Resolves with `undefined` once `signal` aborts, so a hung step cannot hold the budget open. */
function untilAborted<T>(work: Promise<T>, signal: AbortSignal): Promise<T | undefined> {
	return new Promise((resolve) => {
		const stop = () => resolve(undefined);
		if (signal.aborted) return stop();
		signal.addEventListener("abort", stop, { once: true });
		work.then(resolve, () => resolve(undefined)).finally(() => signal.removeEventListener("abort", stop));
	});
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

	/**
	 * The newest successful fetch in this process. It wins over Pi's published state, which lags (or never arrives)
	 * when Pi cannot restore or persist its store. An authoritative empty fetch is kept as empty.
	 */
	let latest: Model<"openai-completions">[] | undefined;

	const discover = async (apiKey: string | undefined, signal: AbortSignal | undefined) => {
		const catalog: Catalog = await fetchCatalog({
			baseUrl: baseUrl(),
			apiKey,
			signal,
			timeoutMs: Number(env().AZIZAM_MODELS_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS,
			fetch: options.fetch,
		});
		if (env().AZIZAM_DEBUG) {
			console.error(`[azizam] ${catalog.models.length} chat models (filter: ${catalog.mode}, skipped malformed: ${catalog.malformed})`);
		}
		latest = catalog.models.map(toModel);
		return latest;
	};

	const provider = createProvider<"openai-completions">({
		id: PROVIDER_ID,
		name: "Azizam",
		auth: { apiKey: envApiKeyAuth("Azizam API key", ["AZIZAM_API_KEY"]) },
		models: [],
		api: openAICompletionsApi(),
		fetchModels: (context) => discover(context.credential?.type === "api_key" ? context.credential.key : undefined, context.signal),
	});

	// Models persisted by an earlier run keep the base URL they were fetched with; always serve the current one.
	const rebased = <T extends { baseUrl: string }>(models: readonly T[]): T[] => {
		const url = baseUrl();
		return models.map((model) => ({ ...model, baseUrl: url }));
	};
	const piModels = () => provider.getModels();
	const piAllModels = () => provider.getAllModels?.() ?? provider.getModels();

	const hasUsablePersistedCatalog = () => {
		const url = baseUrl();
		const models = piAllModels();
		return models.length > 0 && models.every((model) => model.baseUrl === url);
	};

	return {
		...provider,
		getModels: () => rebased(latest ?? piModels()),
		getAllModels: () => rebased(latest ?? piAllModels()),
		prefetch: async (models) => {
			if (env().PI_OFFLINE || process.argv.includes("--offline")) return;
			const signal = AbortSignal.timeout(options.prefetchTimeoutMs ?? PREFETCH_TIMEOUT_MS);
			try {
				// A restore failure (locked, unreadable or malformed store) counts as a miss.
				await untilAborted(models.refresh({ allowNetwork: false, providers: [PROVIDER_ID], signal }), signal);
				if (signal.aborted || hasUsablePersistedCatalog()) return;
				// Pi's credential store needs a writable agent dir even to read; the environment key needs nothing.
				const resolved = await untilAborted(models.getAuth(PROVIDER_ID, { signal }), signal);
				const apiKey = resolved?.auth.apiKey ?? (env().AZIZAM_API_KEY || undefined);
				if (!apiKey || signal.aborted) return;
				await discover(apiKey, signal);
			} catch {
				// Pi's own refresh reports the error; the persisted catalog (if any) still applies.
			}
		},
	};
}

/** What {@link registerProviderScoped} needs from Pi's `ModelRuntime`. */
export interface ScopedRegistrationRuntime {
	refresh(options?: ModelsRefreshOptions): Promise<ModelsRefreshResult>;
	registerNativeProvider(provider: Provider): void;
}

/**
 * Registers `provider` without Pi's side effect. `ModelRuntime.registerNativeProvider()` starts an unawaited,
 * unscoped `refresh()` that checks credentials for every provider Pi knows (including `auth.json` `!command` keys of
 * unrelated providers). The call happens synchronously inside `registerNativeProvider`, so for exactly that window
 * unscoped refreshes become no-ops; provider-scoped refreshes still run. Pi 1.0.x offers no scoped registration.
 */
export function registerProviderScoped(runtime: ScopedRegistrationRuntime, provider: Provider): void {
	const original = Object.getOwnPropertyDescriptor(runtime, "refresh");
	const scoped = runtime.refresh.bind(runtime);
	runtime.refresh = async (refreshOptions) =>
		refreshOptions?.providers ? scoped(refreshOptions) : { aborted: false, errors: new Map() };
	try {
		runtime.registerNativeProvider(provider);
	} finally {
		if (original) Object.defineProperty(runtime, "refresh", original);
		else delete (runtime as Partial<ScopedRegistrationRuntime>).refresh;
	}
}
