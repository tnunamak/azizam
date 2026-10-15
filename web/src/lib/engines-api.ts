/**
 * Add engine: the definitions, the check, the draft test, the access answer
 * and the engine view. The dialog and the engine page call these; a script or
 * an MCP tool calls the same routes (gateway/engine_admin.py).
 */
import {
  adminFetch,
  adminRegistryMutation,
  ApiError,
  type ServiceRegistryResponse,
} from "@/lib/api";

export type FieldType =
  | "text"
  | "url"
  | "secret"
  | "number"
  | "boolean"
  | "select"
  | "multiselect"
  | "list"
  | "keyvalue"
  | "duration";

export interface EngineField {
  key: string;
  label: string;
  type: FieldType;
  help?: string;
  required?: boolean;
  default?: unknown;
  placeholder?: string;
  order?: number;
  group?: string;
  secret?: boolean;
  writeOnly?: boolean;
  show_if?: { field: string; equals: unknown };
  options?: { value: string; label: string }[];
  options_from?: string;
  default_from?: string;
  advanced?: boolean;
  owner_only?: boolean;
  secondary_of?: string;
  unit?: "seconds" | "minutes" | "tokens";
  items?: "transform" | "text";
}

export interface EngineDefinition {
  definition: string;
  definition_version: number;
  title: string;
  example?: string;
  summary: string;
  badge?: string;
  keywords?: string[];
  flow: "detect";
  steps: string[];
  fields: EngineField[];
  check_rows: string[];
  check_request: { method: "GET" | "POST"; path: string; from: "origin" | "address" };
  capabilities: { id: string; label: string }[];
  models_note?: string;
}

export interface EngineDefinitions {
  format_version: number;
  definitions: EngineDefinition[];
  real_tests: Record<string, { sent: string; cost: string }>;
}

export interface FindingAction {
  id: "fixpath" | "openexisting";
  label: string;
  address_append?: string;
  address_strip?: string;
  service_id?: string;
}

export interface Finding {
  code: string;
  severity: "error" | "warning";
  field: "address" | "key";
  title: string;
  what: string;
  why: string;
  fix: string;
  keep_off: boolean;
  summary?: string;
  owner_note?: string;
  action?: FindingAction;
  details?: { raw: string; raw_label: string; request?: string; with_key?: boolean };
}

export type CapabilityLevel =
  | "likely"
  | "possible"
  | "declared"
  | "against"
  | "none"
  | "needs_key";

export interface CheckedCapability {
  id: string;
  label: string;
  level: CapabilityLevel;
  evidence: string;
  why?: string;
  ticked: boolean;
  real_test?: string;
  sent_to?: string;
}

export interface DetectedModel {
  id: string;
  public_name: string;
  context_window: number | null;
  use: string;
}

export interface CheckResult {
  definition: string;
  definition_version: number;
  address: string;
  calls: string;
  checked_at: number;
  outcome: "ok" | "warning" | "error";
  reachable: boolean;
  auth: "unknown" | "accepted" | "not_needed" | "needed" | "rejected";
  key_sent: boolean;
  key_withheld: boolean;
  key_source: "none" | "pasted" | "env" | "stored";
  findings: Finding[];
  capabilities: CheckedCapability[];
  models: DetectedModel[];
  shapes: string[];
  suggested_name: string;
  name_source: "server" | "address" | "";
  steps: { label: string; result: string }[];
}

export interface TestResult {
  capability: string;
  label: string;
  ok: boolean;
  sent: string;
  sent_to: string;
  reply: string;
  seconds: number;
  cost: string;
  at: number;
  status: number | null;
  via: "draft" | "saved";
  key_sent: boolean;
  key_withheld: boolean;
  finding?: { what: string; why: string; fix: string; raw: string };
}

/** One request adjustment as stored: a named built-in or one of the six primitives. */
export type TransformRow =
  | string
  | ({ kind: "value-map"; param: string; map: Record<string, unknown>; default?: unknown } & PhaseFields)
  | ({ kind: "param-map"; from: string; to: string; only_if_destination_absent?: boolean } & PhaseFields)
  | ({ kind: "drop-params"; drop: string[] } & PhaseFields)
  | ({ kind: "default-params"; set: Record<string, unknown> } & PhaseFields)
  | ({ kind: "message-inject"; role: string; text: string; mode?: "prepend" | "append" | "append-or-insert" } & PhaseFields)
  | ({ kind: "clamp"; param: string; min?: number; max?: number } & PhaseFields);

interface PhaseFields {
  phase?: "request" | "response";
  /** Kept as stored; the typed forms do not edit it. */
  when?: unknown;
  [extra: string]: unknown;
}

/** A request header. The value is saved as plain text; a key goes in the API key field. */
export interface HeaderRow {
  name: string;
  value: string;
}

/** Draft values, keyed by the definition's field keys. */
export interface EngineValues {
  address: string;
  api_key?: string;
  api_key_env?: string;
  headers?: HeaderRow[];
  reply_timeout_s?: number | string;
  path_prefix?: string;
  transforms?: TransformRow[];
  lifecycle_mode?: "external" | "managed";
  lifecycle_start?: string;
  lifecycle_stop?: string;
  lifecycle_ready_s?: number | string;
}

export interface StoredTest {
  ok: boolean;
  at: number;
  via: "draft" | "saved";
  seconds?: number;
}

/** One access plan that includes the engine's models, and who holds it. */
export type AccessModel = string | { name: string; capabilities: string[] };

export interface EngineAccessPlan {
  id: string;
  name: string;
  models: string[];
  why: string;
  people: number;
  apps: number;
  link: string;
}

/** Who will be able to use a set of models. Computed by the gateway from the real access plans. */
export interface EngineAccess {
  plans: EngineAccessPlan[];
  you: boolean;
  not_included: string[];
  category_rules: boolean;
  unreadable_plans: number;
  /** The whole answer in one line. */
  sentence: string;
  /** The access plans page. */
  link: string;
}

export interface EngineView {
  service_id: string;
  definition: string;
  definition_version: number;
  /** True when the record predates definitions and the kind was inferred. */
  inferred: boolean;
  name: string;
  enabled: boolean;
  /** The gateway runs this engine itself; only name and Enabled change. */
  locked: boolean;
  lifecycle_editable: boolean;
  /** False when the model router does not call this engine at its own address: settings are read-only. */
  editable: boolean;
  not_editable_reason: string;
  values: EngineValues;
  key: { state: "none" | "configured"; source?: "stored" | "env"; env_name?: string; origin?: string };
  capabilities: string[];
  models: { id: string; public_name: string; context_window: number | null; enabled: boolean; use?: string }[];
  tests: Record<string, StoredTest>;
  check: {
    status?: "ok" | "failed" | "not_started";
    reason?: "needkey";
    checked_at?: number;
    origin?: string;
    key_sent?: boolean;
    model_count?: number;
    /** What the last stored check found for each capability. */
    capabilities?: { id: string; level: CapabilityLevel; evidence: string; why?: string }[];
    /** What each model is for, by model id, from the last stored check. */
    model_use?: Record<string, string>;
  };
  /** Who can use the turned-on models, as saved. */
  access: EngineAccess;
  calls: string;
  other_settings: Record<string, unknown>;
  /** The stored record, secrets shown as "configured". */
  record: Record<string, unknown>;
}

export interface CreateEngineBody {
  definition: string;
  name: string;
  values: EngineValues;
  capabilities?: string[];
  models?: { id: string; public_name: string; context_window?: number | null; enabled?: boolean }[];
  tests?: Record<string, { ok: boolean; at: number; seconds?: number }>;
  acknowledge_warnings?: boolean;
  disabled?: boolean;
  /** Why it is kept off, when the reason is the owner's to fix: "needkey". */
  keep_off_reason?: "needkey";
}

export interface UpdateEngineBody extends Partial<CreateEngineBody> {
  enabled?: boolean;
  recheck?: boolean;
}

/** The check a refused create or edit carries (422 errors, 409 warnings or a duplicate). */
export function checkFromError(error: unknown): CheckResult | null {
  if (!(error instanceof ApiError)) return null;
  const payload = error.payload as { check?: CheckResult } | undefined;
  return payload?.check ?? null;
}

export function fieldFromError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const payload = error.payload as { error?: { field?: string } } | undefined;
  return payload?.error?.field ?? null;
}

export const enginesApi = {
  definitions: () => adminFetch<EngineDefinitions>("/admin/api/engine-definitions"),
  check: (body: { definition: string; values: EngineValues; service_id?: string }, signal?: AbortSignal) =>
    adminFetch<{ check: CheckResult }>("/admin/api/engines/check", { method: "POST", body, signal }).then((r) => r.check),
  test: (body: {
    definition: string;
    values: EngineValues;
    capability: string;
    model?: string;
    service_id?: string;
  }) => adminFetch<{ test: TestResult }>("/admin/api/engines/test", { method: "POST", body }).then((r) => r.test),
  /** Who could use these models (capability ids and public names). Saves nothing. */
  /** A model is its public name, or the name with the work the check found it does. */
  access: (body: { capabilities: string[]; models: AccessModel[] }) =>
    adminFetch<{ access: EngineAccess }>("/admin/api/engines/access", { method: "POST", body }).then((r) => r.access),
  view: (serviceId: string) =>
    adminFetch<{ engine: EngineView }>(`/admin/api/engines/${encodeURIComponent(serviceId)}`).then((r) => r.engine),
  /** Answers with the registry and the id the gateway gave the new engine. */
  create: (body: CreateEngineBody) =>
    adminRegistryMutation<ServiceRegistryResponse & { service_id: string }>("/admin/api/operator/services", {
      method: "POST",
      body,
    }).then((r) => ({ registry: r.service_registry, serviceId: r.service_id })),
  update: (serviceId: string, body: UpdateEngineBody) =>
    adminRegistryMutation<ServiceRegistryResponse>(`/admin/api/operator/services/${encodeURIComponent(serviceId)}`, {
      method: "PATCH",
      body,
    }).then((r) => r.service_registry),
};
