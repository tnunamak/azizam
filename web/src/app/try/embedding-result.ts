export interface EmbeddingSummary {
  model: string | null;
  inputs: number;
  dimensions: number;
  preview: number[];
  more: number;
  json: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Describe the first embedding without inferring a model or rounding its values. */
export function summarizeEmbedding(payload: unknown): EmbeddingSummary | null {
  if (!isRecord(payload) || !Array.isArray(payload.data) || payload.data.length === 0) {
    return null;
  }
  const first: unknown = payload.data[0];
  if (!isRecord(first) || !Array.isArray(first.embedding) || first.embedding.length === 0) {
    return null;
  }
  // Iteration also rejects holes: Array.every alone would silently skip them.
  const embedding: number[] = [];
  for (const value of first.embedding) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    embedding.push(value);
  }
  const preview = embedding.slice(0, 8);
  return {
    model: typeof payload.model === "string" ? payload.model : null,
    inputs: payload.data.length,
    dimensions: embedding.length,
    preview,
    more: embedding.length - preview.length,
    json: JSON.stringify(embedding),
  };
}
