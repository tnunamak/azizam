// Pure mapping from an Azizam `GET /v1/models` payload to chat-model descriptors.
// No Pi imports: this module is unit-testable on its own. Rules: Azizam model-metadata contract v1, "Client rules".

export const DEFAULT_CONTEXT_WINDOW = 128_000;
export const DEFAULT_MAX_TOKENS = 8_192;

export type InputModality = "text" | "image";

export interface ModelCost {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
}

export interface ChatModelInfo {
	id: string;
	name: string;
	reasoning: boolean;
	input: InputModality[];
	contextWindow: number;
	maxTokens: number;
	cost: ModelCost;
}

/** How the chat list was selected, from most to least precise. */
export type FilterMode = "contract" | "output-modalities" | "unfiltered";

export interface Catalog {
	mode: FilterMode;
	models: ChatModelInfo[];
	/** Entries skipped: not an object with a non-empty string `id`, or a duplicate id. */
	malformed: number;
}

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);

const positiveInt = (value: unknown): number | undefined =>
	typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : undefined;

const stringArray = (value: unknown): string[] | undefined =>
	Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : undefined;

/** Per-token price (number or numeric string) -> USD per million tokens. */
const perMillion = (value: unknown): number | undefined => {
	const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
	return typeof n === "number" && Number.isFinite(n) && n >= 0 ? n * 1_000_000 : undefined;
};

/** Cost is 0 unless the entry carries Vercel-style (`input`/`output`) or OpenRouter-style (`prompt`/`completion`) per-token `pricing`. */
function readCost(pricing: unknown): ModelCost {
	const p = isObject(pricing) ? pricing : {};
	return {
		input: perMillion(p.input ?? p.prompt) ?? 0,
		output: perMillion(p.output ?? p.completion) ?? 0,
		cacheRead: perMillion(p.input_cache_read) ?? 0,
		cacheWrite: perMillion(p.input_cache_write) ?? 0,
	};
}

function readInput(architecture: unknown): InputModality[] {
	const modalities = isObject(architecture) ? stringArray(architecture.input_modalities) : undefined;
	const supported = (["text", "image"] as const).filter((modality) => modalities?.includes(modality));
	return supported.length > 0 ? supported : ["text"];
}

function toChatModel(entry: Json & { id: string }): ChatModelInfo {
	const contextWindow = positiveInt(entry.context_window) ?? DEFAULT_CONTEXT_WINDOW;
	const maxTokens = Math.min(positiveInt(entry.max_tokens) ?? DEFAULT_MAX_TOKENS, contextWindow);
	return {
		id: entry.id,
		name: entry.id,
		reasoning: stringArray(entry.tags)?.includes("reasoning") ?? false,
		input: readInput(entry.architecture),
		contextWindow,
		maxTokens,
		cost: readCost(entry.pricing),
	};
}

function outputsOnlyText(entry: Json): boolean {
	const out = isObject(entry.architecture) ? stringArray(entry.architecture.output_modalities) : undefined;
	return out?.length === 1 && out[0] === "text";
}

/**
 * Select and map chat models from a `/v1/models` response body.
 * - Any entry has `type`: contract gateway; keep `type === "language"` (entries without `type` are unknown, so dropped).
 * - No `type` anywhere, but `architecture.output_modalities` present: keep entries whose outputs are exactly ["text"].
 * - No metadata at all: keep everything.
 * Malformed entries are skipped. Throws only when the body is not `{ data: [...] }`.
 */
export function parseModelList(body: unknown): Catalog {
	const data = isObject(body) ? body.data : undefined;
	if (!Array.isArray(data)) throw new Error("response has no `data` array (is this an OpenAI-compatible /models endpoint?)");

	const seen = new Set<string>();
	const entries: (Json & { id: string })[] = [];
	for (const item of data) {
		if (!isObject(item) || typeof item.id !== "string" || item.id === "") continue;
		if (seen.has(item.id)) continue;
		seen.add(item.id);
		entries.push(item as Json & { id: string });
	}
	const malformed = data.length - entries.length;

	let mode: FilterMode;
	let selected: (Json & { id: string })[];
	if (entries.some((e) => typeof e.type === "string")) {
		mode = "contract";
		selected = entries.filter((e) => e.type === "language");
	} else if (entries.some((e) => isObject(e.architecture) && Array.isArray(e.architecture.output_modalities))) {
		mode = "output-modalities";
		selected = entries.filter(outputsOnlyText);
	} else {
		mode = "unfiltered";
		selected = entries;
	}
	return { mode, models: selected.map(toChatModel), malformed };
}
