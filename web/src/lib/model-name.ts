/**
 * Model names for people. A raw model id can be a file on the gateway's disk
 * (a llama.cpp engine reports the GGUF it loaded). No screen shows that path:
 * the identifier shrinks to the file name, and the display name reads as words.
 */

const MODEL_FILE_EXTENSION = /\.(?:gguf|safetensors|ckpt|onnx|bin)$/i;
const PATH_START = /^(?:\/|~[\\/]|[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/])/;

/** True for an absolute, home-relative or drive path, or a bare model file. */
export function isPathLikeModelId(id: string): boolean {
  const value = id.trim();
  return PATH_START.test(value) || MODEL_FILE_EXTENSION.test(value);
}

/** The last path segment: "/a/b/Model-Q4.gguf" -> "Model-Q4.gguf". */
function lastSegment(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/**
 * The identifier a screen may show or copy: unchanged, except that a path
 * becomes its short file name. Directories on the gateway host stay private.
 */
export function safeModelId(id: string): string {
  return isPathLikeModelId(id) ? lastSegment(id.trim()) : id;
}

/** "/x/Qwen3.8-27B-Q4_K_M.gguf" -> "Qwen3.8 27B Q4 K M". */
function readableFileName(id: string): string {
  return lastSegment(id.trim())
    .replace(MODEL_FILE_EXTENSION, "")
    .replace(/-\d{5}-of-\d{5}$/, "")
    .split(/[-_\s]+/)
    .filter(Boolean)
    .join(" ");
}

/**
 * A readable name from a raw model id: "krea2-turbo" -> "Krea2 Turbo",
 * "tts-1" -> "TTS 1". Short all-letter words read as acronyms.
 */
export function prettifyModelId(id: string): string {
  return id
    .split(/[-_./:\s]+/)
    .filter(Boolean)
    .map((word) =>
      /^[a-z]{1,3}$/i.test(word)
        ? word.toUpperCase()
        : word[0].toUpperCase() + word.slice(1),
    )
    .join(" ");
}

interface CatalogNameRow {
  id: string;
  title?: string | null;
  display_name?: string | null;
}

export interface CapabilityNameRow {
  recommended_model?: string | null;
  use_name?: string | null;
  use_names?: Array<string> | null;
  model?: string | null;
}

/**
 * The name a person reads for a model: the catalog's title, else the
 * recommended name of a capability that answers to this id, else the id made
 * readable. The raw id stays in Details as "Model name".
 */
export function modelDisplayName(
  model: string,
  catalog?: { aliases?: CatalogNameRow[]; models?: CatalogNameRow[] },
  capabilities: CapabilityNameRow[] = [],
): string {
  const row = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])].find(
    (item) => item.id === model,
  );
  const titled = [row?.title, row?.display_name].find(
    (name) => name && name.trim() && name !== model,
  );
  if (titled) {
    return isPathLikeModelId(titled) ? readableFileName(titled) : titled;
  }
  const capability = capabilities.find(
    (item) =>
      item.recommended_model &&
      [item.use_name, item.model, ...(item.use_names ?? [])].includes(model),
  );
  if (capability?.recommended_model) return capability.recommended_model;
  return isPathLikeModelId(model)
    ? readableFileName(model)
    : prettifyModelId(model);
}


const MODEL_KEY = /model|display_name|resolves_to|^(?:id|name|title)$/i;

/**
 * A payload with every model-naming field that holds a file path shortened to
 * the file name. Fields that merely look like paths (a URL path) are left alone:
 * only keys that name a model are touched.
 */
export function scrubModelPaths<T>(value: T, key = "", inModel = false): T {
  const naming = inModel || MODEL_KEY.test(key);
  if (typeof value === "string") {
    return (naming ? safeModelId(value) : value) as T;
  }
  if (Array.isArray(value)) {
    return value.map((item) => scrubModelPaths(item, key, naming)) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([name, item]) => [
        name,
        // Under a model-naming object like resolves_to, every value is a model.
        scrubModelPaths(item, name, /resolves_to/i.test(key) ? true : false),
      ]),
    ) as T;
  }
  return value;
}
