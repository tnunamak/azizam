/**
 * Model names for people. A raw model id can be a file on the gateway's disk.
 * Member-facing identifiers omit directories and display names read as words.
 * Only owner technical details retain the exact identifier for routing diagnostics.
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

/**
 * A weight file's precision or quantisation tag at the end of its name
 * ("-Q4_K_M", "-IQ4_XS", ".f16", "_fp8_scaled"): how the file was packed,
 * not which model it is.
 */
const PACKING_SUFFIX =
  /[-_.](?:i?q\d(?:_[a-z0-9]+)*|b?f(?:p)?(?:8|16|32)(?:_[a-z0-9]+)*|int[48](?:_[a-z0-9]+)*|mxfp4|awq|gptq|exl2)$/i;

/** True for a name that is a weight file's stem rather than a model's name. */
function isFileStem(name: string): boolean {
  return PACKING_SUFFIX.test(name.trim());
}

/** "/x/Qwen3.8-27B-Uncensored-Q4_K_M.gguf" -> "Qwen3.8 27B Uncensored". */
function readableFileName(id: string): string {
  let stem = lastSegment(id.trim())
    .replace(MODEL_FILE_EXTENSION, "")
    .replace(/-\d{5}-of-\d{5}$/, "");
  for (let next = stem.replace(PACKING_SUFFIX, ""); next !== stem && next; ) {
    stem = next;
    next = stem.replace(PACKING_SUFFIX, "");
  }
  return stem.split(/[-_\s]+/).filter(Boolean).join(" ");
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
  workflow?: string | null;
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
  workflow?: string | null,
): string {
  const rows = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])];
  const row = rows.find((item) => item.id === model) ?? rows.find(
    (item) => item.workflow && item.workflow === (workflow || model),
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
      // Grouped capabilities pool every workflow's use_names. Those names
      // share a capability, not the recommended model's identity.
      [item.use_name, item.model].includes(model),
  );
  if (capability?.recommended_model) {
    const name = capability.recommended_model;
    return isPathLikeModelId(name) || isFileStem(name)
      ? readableFileName(name)
      : name;
  }
  return isPathLikeModelId(model) || isFileStem(model)
    ? readableFileName(model)
    : prettifyModelId(model);
}


/** Owners can inspect exact routing IDs; members receive scrubbed copies. */
export function technicalModelDetails<T>(value: T, owner: boolean): T {
  return owner ? value : scrubModelPaths(value);
}

const MODEL_KEY = /model|display_name|resolves_to|fallback|^(?:id|name|title)$/i;

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
