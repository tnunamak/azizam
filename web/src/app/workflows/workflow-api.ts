export interface WorkflowInput {
  name: string;
  type: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  enum?: unknown[];
  required?: boolean;
  accepted: boolean;
  description?: string;
}

export interface Workflow {
  id: string;
  capability: string;
  title: string;
  description: string;
  inputs: WorkflowInput[];
  engine_binding: string | null;
  availability: { status: string; reason: string; available?: boolean };
  typical_seconds: number | null;
  aliases: string[];
  examples: { curl: string; python: string; javascript: string };
  native_route: string | null;
  discovery_url: string;
  try_url?: string;
}

export interface WorkflowCatalog {
  schema_version: number;
  workflows: Workflow[];
  counts?: { workflows: number; capabilities: number };
}

export interface RecentAsset {
  asset_id: string;
  job_id?: string | null;
  kind: string;
  prompt: string;
  model: string;
  workflow: string | null;
  created_at: number | string | null;
  generation_ms?: number | null;
  download_url?: string | null;
  receipt_id?: string | null;
}

export async function readJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok)
    throw new Error("Workflow data could not be loaded. Try again.");
  return response.json() as Promise<T>;
}
