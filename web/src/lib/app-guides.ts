import data from "./app-guides.json";

/**
 * How to connect each app, as data (app-guides.json). Pages render every
 * entry the same way; adding an app is a new JSON entry, never new code.
 */

export type GuideStatus = "documented" | "unverified";

export type GuideField = {
  label: string;
  value: string;
  /** Fields that share a group sit under one small heading (OpenWhispr). */
  group?: string;
  note?: string;
};

export type KeyGuide = {
  status: GuideStatus;
  steps: string[];
  fields: GuideField[];
  snippet?: { language: string; code: string };
  note?: string;
  docs: string;
};

export type McpGuide = {
  auth: "sign-in" | "sign-in-or-key";
  status: GuideStatus;
  steps: string[];
  note?: string;
  docs: string;
};

export type AppGuide = {
  id: string;
  name: string;
  kind: string;
  key?: KeyGuide;
  mcp?: McpGuide;
};

export const appGuides: { checked: string; apps: AppGuide[] } = data as {
  checked: string;
  apps: AppGuide[];
};

/** Apps that connect with an app key, in the data's order. */
export const keyApps = appGuides.apps.filter(
  (app): app is AppGuide & { key: KeyGuide } => Boolean(app.key),
);

/** Apps that connect to the MCP server. */
export const mcpApps = appGuides.apps.filter(
  (app): app is AppGuide & { mcp: McpGuide } => Boolean(app.mcp),
);

export function keyAppById(id: string | null | undefined) {
  return keyApps.find((app) => app.id === id);
}

/** The values a guide's placeholders take. A null value is not available. */
export type GuideValues = {
  openai_url: string;
  anthropic_url: string;
  mcp_url: string;
  chat_model: string | null;
  transcription_model: string | null;
};

const placeholders = [
  "openai_url",
  "anthropic_url",
  "mcp_url",
  "chat_model",
  "transcription_model",
] as const;

/** What a missing model placeholder needs, in the words of the Models page. */
const modelTasks: Partial<Record<(typeof placeholders)[number], string>> = {
  chat_model: "chat",
  transcription_model: "transcription",
};

// A code snippet takes a value only when it cannot break out of its quotes.
const safeInCode = /^[\w.:/@+~%=-]+$/;

function fill(
  text: string,
  values: GuideValues,
  inCode: boolean,
): { text: string; missing: string[] } {
  const missing: string[] = [];
  let out = text;
  for (const name of placeholders) {
    const token = `{${name}}`;
    if (!out.includes(token)) continue;
    const value = values[name];
    if (!value) {
      missing.push(modelTasks[name] ?? name);
      out = out.replaceAll(token, inCode ? "MODEL_NAME" : "");
    } else if (inCode && !safeInCode.test(value)) {
      out = out.replaceAll(token, "MODEL_NAME");
    } else {
      out = out.replaceAll(token, value);
    }
  }
  return { text: out, missing };
}

export type ResolvedField = GuideField & {
  /** True where the app key goes; the value is then empty. */
  isKey: boolean;
  /** True for the gateway address ({openai_url} and the like). */
  isAddress: boolean;
  /** True for a model name ({chat_model} and the like). */
  isModel: boolean;
  /** True when the value needs a model this person has none of. */
  missing: boolean;
};

export type ResolvedKeyGuide = Omit<KeyGuide, "fields" | "snippet" | "steps"> & {
  steps: string[];
  fields: ResolvedField[];
  snippet?: { language: string; code: string };
  /** The field label the app uses for the key ("API Key", "GATEWAY_API_KEY"). */
  keyLabel: string;
  /** Tasks this app needs a model for and the person has none ("transcription"). */
  missingTasks: string[];
};

export function resolveKeyGuide(
  guide: KeyGuide,
  values: GuideValues,
): ResolvedKeyGuide {
  const missingTasks = new Set<string>();
  const fields = guide.fields.map((field) => {
    if (field.value === "{key}") {
      return { ...field, value: "", isKey: true, isAddress: false, isModel: false, missing: false };
    }
    const { text, missing } = fill(field.value, values, false);
    missing.forEach((task) => missingTasks.add(task));
    return {
      ...field,
      value: text,
      isKey: false,
      isAddress: /\{\w+_url\}/.test(field.value),
      isModel: /\{\w+_model\}/.test(field.value),
      missing: missing.length > 0,
    };
  });
  const snippet = guide.snippet
    ? { ...guide.snippet, code: fill(guide.snippet.code, values, true).text }
    : undefined;
  return {
    ...guide,
    steps: guide.steps.map((step) => fill(step, values, false).text),
    fields,
    snippet,
    keyLabel: fields.find((field) => field.isKey)?.label ?? "API key",
    missingTasks: [...missingTasks],
  };
}

export function resolveMcpGuide(guide: McpGuide, mcpUrl: string): McpGuide {
  const values: GuideValues = {
    openai_url: "",
    anthropic_url: "",
    mcp_url: mcpUrl,
    chat_model: null,
    transcription_model: null,
  };
  return { ...guide, steps: guide.steps.map((step) => fill(step, values, false).text) };
}

/** The smallest shape of a catalog row a guide needs to pick a model. */
type ModelChoice = {
  id: string;
  capability?: string;
  capabilities: { capability?: string }[];
  recommended?: boolean;
  locality?: string;
  alias?: boolean;
  availability?: { available?: boolean };
};

/**
 * The model a guide suggests for a task: the gateway's recommendation, else a
 * local model (so a guide does not quietly spend on a provider), else any. A
 * name that points at a model (`fast`) beats the model itself, because it keeps
 * working when the model behind it changes; a name known to be unavailable is
 * skipped.
 */
export function suggestedModel(
  rows: ModelChoice[],
  task: "chat" | "transcribe",
): string | null {
  const ofTask = rows.filter(
    (row) =>
      (row.capability ?? row.capabilities[0]?.capability) === task &&
      row.availability?.available !== false,
  );
  const names = ofTask.filter((row) => row.alias);
  const matches = names.length > 0 ? names : ofTask;
  return (
    matches.find((row) => row.recommended)?.id ??
    matches.find((row) => row.locality === "local")?.id ??
    matches[0]?.id ??
    null
  );
}
