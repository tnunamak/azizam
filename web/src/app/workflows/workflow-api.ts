import type { WorkflowAvailability } from "@/lib/workflow-availability";

export interface WorkflowInput {
  name: string;
  type: string;
  media_type?: string;
  accepted_media_types?: string[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  enum?: unknown[];
  required?: boolean;
  accepted: boolean;
  description?: string;
  /** Set when the manifest declares a media file; its JSON type is still "string". */
  asset?: boolean;
  /** Short noun phrase the manifest declares, such as "a face photo". */
  label?: string;
}

export interface WorkflowOutput {
  name: string;
  media_type?: string;
  description?: string;
  label?: string;
}

export interface Workflow {
  id: string;
  capability: string;
  title: string;
  purpose?: string;
  description: string;
  technical_notes?: string;
  inputs: WorkflowInput[];
  outputs?: WorkflowOutput[];
  engine_binding: string | null;
  availability: WorkflowAvailability & {
    status: string;
    reason_code: string;
  };
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
  prompt: string | null;
  model: string | null;
  workflow: string | null;
  created_at: number | string | null;
  generation_ms?: number | null;
  download_url?: string | null;
  request_id?: string | null;
}

export async function readJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok)
    throw new Error("Workflow data could not be loaded. Try again.");
  return response.json() as Promise<T>;
}

// The page validates only what it reads: a shape that would crash a render
// becomes the retryable load error. Optional fields may be absent or null.
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
const string = (value: unknown): value is string => typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(string);
const optionalString = (value: unknown) => value == null || string(value);
const optionalNumber = (value: unknown) => value == null || (typeof value === "number" && Number.isFinite(value));
const optionalBoolean = (value: unknown) => value == null || typeof value === "boolean";
function input(value: unknown): boolean {
  return record(value) && string(value.name) && string(value.type) &&
    typeof value.accepted === "boolean" && optionalBoolean(value.required) &&
    optionalBoolean(value.asset) && optionalString(value.label) &&
    optionalString(value.description) && optionalString(value.media_type) &&
    (value.accepted_media_types == null || strings(value.accepted_media_types)) &&
    optionalNumber(value.minimum) && optionalNumber(value.maximum) &&
    (value.enum == null || Array.isArray(value.enum));
}
function output(value: unknown): boolean {
  return record(value) && string(value.name) && optionalString(value.label) &&
    optionalString(value.media_type) && optionalString(value.description);
}
function workflow(value: unknown): boolean {
  if (!record(value)) return false;
  const availability = value.availability;
  const examples = value.examples;
  return ["id", "capability", "title", "description"].every((key) => string(value[key])) &&
    ["purpose", "technical_notes", "engine_binding", "native_route", "try_url", "discovery_url"]
      .every((key) => optionalString(value[key])) &&
    Array.isArray(value.inputs) && value.inputs.every(input) &&
    (value.outputs == null || (Array.isArray(value.outputs) && value.outputs.every(output))) &&
    strings(value.aliases) && optionalNumber(value.typical_seconds) &&
    record(availability) && string(availability.status) && optionalString(availability.reason_code) &&
    ["available", "measured", "configured", "enabled", "up", "idle_startable"]
      .every((key) => optionalBoolean(availability[key])) &&
    record(examples) && ["curl", "python", "javascript"].every((key) => string(examples[key]));
}
export function isWorkflowCatalog(value: unknown): value is WorkflowCatalog {
  return record(value) && value.schema_version === 1 &&
    Array.isArray(value.workflows) && value.workflows.every(workflow);
}
function isRecentAssets(value: unknown): value is { assets: RecentAsset[] } {
  // A prompt that was not kept and an unrecorded model arrive as null.
  return record(value) && Array.isArray(value.assets) && value.assets.every((asset) =>
    record(asset) && string(asset.asset_id) && string(asset.kind) &&
    ["prompt", "model", "job_id", "workflow", "download_url", "request_id"]
      .every((key) => optionalString(asset[key])) &&
    optionalNumber(asset.generation_ms) &&
    (asset.created_at == null || string(asset.created_at) || optionalNumber(asset.created_at)));
}
export async function readRecentAssets(path: string): Promise<{ assets: RecentAsset[] }> {
  const value = await readJson<unknown>(path);
  if (!isRecentAssets(value)) throw new Error("Recent creations could not be loaded. Try again.");
  return value;
}
