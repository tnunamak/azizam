export type DetectedModelRow = {
  upstream: string;
  publicName: string;
  selected: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function normalizeModelConfigForPatch(value: unknown): unknown {
  if (Array.isArray(value)) return value;
  if (!isRecord(value)) return value;
  return Object.entries(value).map(([modelName, modelConfig]) => {
    if (isRecord(modelConfig)) {
      const id = modelConfig.id || modelName;
      const normalized: Record<string, unknown> = {
        ...modelConfig,
        id,
        model_group: modelConfig.model_group || id,
        upstream_model:
          modelConfig.upstream_model ||
          modelConfig.deployment ||
          modelConfig.model ||
          id,
      };
      delete normalized.public;
      delete normalized.upstream;
      return normalized;
    }
    return {
      id: modelName,
      model_group: modelName,
      upstream_model: String(modelConfig || modelName),
    };
  });
}

/** Append mappings without replacing an operator's aliases or request policies. */
export function detectedModelsPatch(
  existing: unknown,
  rows: DetectedModelRow[],
) {
  const normalized = normalizeModelConfigForPatch(existing) ?? [];
  if (!Array.isArray(normalized) || normalized.some((row) => !isRecord(row))) {
    throw new Error(
      "Existing model settings need review in Advanced fields before applying detected models.",
    );
  }
  const models = normalized.map((row) => ({ ...row })) as Record<
    string,
    unknown
  >[];
  const selected = rows.filter((row) => row.selected);
  if (!selected.length) throw new Error("Select at least one detected model.");
  const seen = new Set<string>();
  for (const row of selected) {
    const name = row.publicName.trim();
    if (!name || name === "*" || /\s/.test(name)) {
      throw new Error(
        "Public names must be nonempty, contain no spaces, and cannot be *.",
      );
    }
    const key = name.toLowerCase();
    if (seen.has(key))
      throw new Error(
        `Public name ${name} is used more than once in this preview.`,
      );
    seen.add(key);
    const matches = models.filter((model) =>
      [model.id, model.model_group].some(
        (value) => String(value ?? "").toLowerCase() === key,
      ),
    );
    if (matches.length) {
      if (matches.length !== 1 || matches[0].upstream_model !== row.upstream) {
        throw new Error(
          `Public name ${name} is already configured. Choose another name to preserve its routing.`,
        );
      }
      continue;
    }
    models.push({
      id: name,
      model_group: name,
      upstream_model: row.upstream,
      transforms: [],
    });
  }
  return { models, model_policy_schema_version: 1 };
}
