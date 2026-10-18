/** Typed API client for the gateway JSON contract
 * (docs/design/web-api-contract.md). One `adminFetch` injects the CSRF
 * headers on writes; every non-2xx response becomes a typed `ApiError`
 * parsed from the `{"error":{code,message}}` envelope.
 *
 * Auth model: same-origin session cookie. Writes additionally require
 *   x-azizam-admin-action: 1
 *   x-azizam-csrf: <token from GET /admin/api/session>
 * The CSRF token is fetched lazily, cached, and refreshed once on a
 * 403 admin_csrf_required (rolled session). No secret values are ever
 * logged by this module. */

import { networkFetch } from "./network-error";
import { QueryClient } from "@tanstack/react-query";

const SERVICE_REGISTRY_PATH = "/admin/api/operator/service-registry";
let serviceRegistryEtag: string | null = null;

function isServiceRegistryMutation(path: string, method: string): boolean {
  if (method === "PUT" && path === SERVICE_REGISTRY_PATH) return true;
  if (method === "POST" && path === "/admin/api/operator/services") return true;
  if (
    (method === "PATCH" || method === "DELETE") &&
    /^\/admin\/api\/operator\/services\/[^/]+$/.test(path)
  ) {
    return true;
  }
  if (
    method === "POST" &&
    /^\/admin\/api\/operator\/services\/[^/]+\/enabled$/.test(path)
  ) {
    return true;
  }
  return method === "POST" && path === "/admin/api/extensions/enable";
}

function isServiceRegistryResponse(path: string, method: string): boolean {
  return (
    path === SERVICE_REGISTRY_PATH || isServiceRegistryMutation(path, method)
  );
}

function hasHeader(
  headers: Record<string, string> | undefined,
  name: string,
): boolean {
  return Object.keys(headers ?? {}).some(
    (key) => key.toLowerCase() === name.toLowerCase(),
  );
}

// ---------------------------------------------------------------------------
// Error envelope

export type CredentialSubjectKind = "app" | "service_account" | "user";

export interface Credential {
  id: string;
  subject_kind: CredentialSubjectKind;
  subject_id: string;
  app_id: string | null;
  /** First 18 chars of the full key — render via <SecretMask> only. */
  prefix: string;
  display_name: string;
  scopes: string[];
  created_at: number;
  created_by?: string | null;
  expires_at: number | null;
  revoked_at: number | null;
  last_used_at: number | null;
}

/** Mint responses carry the full key exactly once. Reveal-once: show in the
 * reveal dialog, never log, never refetchable. */
export interface MintedCredential extends Credential {
  api_key: string;
  role?: string;
  service_account?: string;
  /** Whose prompts this key carries: a person, or none (service and
   * someone else's Apps), with the gateway's own description. */
  prompt_memory?: { person_id: string | null; description?: string };
}

export interface ApiErrorEnvelope {
  error: { code: string; message: string };
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  /** The whole error body, for routes that answer with more than code and message. */
  readonly payload?: unknown;

  constructor(code: string, message: string, status: number, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.payload = payload;
  }
}

function isErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const error = (value as { error?: unknown }).error;
  if (typeof error !== "object" || error === null) return false;
  const { code, message } = error as { code?: unknown; message?: unknown };
  return typeof code === "string" && typeof message === "string";
}

// ---------------------------------------------------------------------------
// Contract types (verified against web-api-contract.md, 2026-06-11)

export interface SessionActor {
  kind?: "user" | "service" | string;
  label?: string | null;
  subject: string;
  email: string | null;
  roles: string[];
  /** Empty string for bearer-key callers (no CSRF needed there). */
  csrf_token: string;
}

export interface Session {
  actor: SessionActor;
}

/** An app as listed by the owner admin API. */
export interface App {
  id: string;
  kind: "service" | "personal";
  name: string;
  /** The accountable person: their plan bounds the app (design §2.4). */
  owner_user_id: string;
  accountable_person: { id: string; name: string | null } | null;
  /** An access plan id, "person" (same as its person), or null: no plan, denies. */
  access_plan: string | null;
  same_as_person: boolean;
  access_plan_name: string | null;
  effective_access_plan_id: string | null;
  created_at: number;
  disabled_at: number | null;
}

export interface User {
  id: string;
  issuer: string;
  subject: string;
  email: string | null;
  display_name: string | null;
  name: string | null;
  created_at: number;
  last_login_at: number | null;
  roles: string[];
}

export type CapabilityId =
  | "chat" | "image" | "video" | "speech" | "transcribe" | "music"
  | "sfx" | "audio" | "decide" | "embed" | "rerank";

export type PlanItem = { model: string } | { workflow: string };

export interface PlanCaps {
  usd_per_request: string | null;
  usd_per_day: string | null;
  usd_per_month: string | null;
}

/** An access plan document (design §3.1). Every field is required. */
export interface AccessPlanDocument {
  id?: string;
  name: string;
  can_use: Partial<Record<CapabilityId, { items: "all" | PlanItem[] }>>;
  direct_engine_access: { engines: "all" | string[]; operations: string[] };
  paid_providers: string[];
  provider_fallback: boolean;
  /** US dollars as decimal strings; null means no cap for that period. */
  caps: PlanCaps;
  can_load_models: boolean;
  receipt_detail: "none" | "basic" | "standard" | "full";
  media_metadata: "none" | "full";
}

export interface AccessPlan {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  plan: AccessPlanDocument;
  summary: string | null;
  used_by?: { people: string[]; apps: string[]; invites: number };
}

/** A plan as pickers show it. */
export interface AccessPlanChoice {
  id: string;
  name: string;
  summary: string | null;
}

export interface PolicyEditorChoice {
  id: string;
  label: string;
  description: string | null;
  available?: boolean;
}

export interface AccessPlanEditorContract {
  schema_version: 2;
  capabilities: {
    id: CapabilityId;
    label: string;
    items: { kind: "model" | "workflow"; id: string; available: boolean }[];
  }[];
  engines: { id: string; label: string; available: boolean }[];
  paid_providers: { id: string; label: string; available: boolean }[];
  receipt_detail: PolicyEditorChoice[];
  media_metadata: PolicyEditorChoice[];
  new_plan: AccessPlanDocument;
}

// ---------------------------------------------------------------------------
// Services

export interface ServiceModelMapping {
  id?: string;
  model_group: string;
  upstream_model: string;
  base_model_id?: string;
}

export interface ServiceResources {
  gpu_required?: boolean;
  max_parallel?: number;
  vram_gb_estimate?: number;
  duration_class?: string;
}

export interface ServiceConfig {
  host?: string;
  port?: number;
  base_path?: string;
  base_url?: string;
  execution_substrate?: string;
  protocol?: string;
  health_endpoint?: string;
  timeout?: number;
  native_passthrough?: boolean;
  api_key_env?: string;
  note?: string;
  resources?: ServiceResources;
  models?: ServiceModelMapping[];
  [key: string]: unknown;
}

export type ServiceSource =
  | "provisioned"
  | "ui"
  | "builtin"
  | "imported"
  | "discovered";

export interface Service {
  id: string;
  name: string;
  display_name?: string | null;
  kind: string;
  source: ServiceSource;
  enabled: boolean;
  editable: boolean;
  capabilities: string[];
  role?: string;
  priority?: number;
  config: ServiceConfig;
  secret_refs?: Record<string, string>;
}

export interface ServiceRegistry {
  schema_version: number;
  services: Record<string, Service>;
  protocol_frontends?: Record<string, unknown>;
  workflows?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
}

export interface ServiceRegistryResponse {
  service_registry: ServiceRegistry;
}

// ---------------------------------------------------------------------------
// Check access

/**
 * The rule that decided a row, allowed or not. `code` is stable (branch on it,
 * never on the sentence). `id` and `name` say what to edit: a plan, or a category
 * (an administrator is not given a category's id or name).
 */
export interface AccessRule {
  code: string;
  kind: "plan" | "category" | "provider";
  id: string | null;
  name: string | null;
  /** The rule in plain English; never an internal id. */
  plain_reason: string;
}

/** One kind of work (Chat, Images) an entry offers, and whether the plan allows it. */
export interface AccessPreviewKind {
  kind: string;
  label: string;
  allowed: boolean;
  /** "unknown" means the answer depends on something that could not be read. */
  state: "allowed" | "blocked" | "unknown";
  reason: string | null;
  rule: AccessRule | null;
}

/** One Models page entry (a model, an alias or a workflow) and what the plan decides for it. */
export interface AccessPreviewEntry {
  id: string;
  name: string;
  alias: boolean;
  group: string;
  group_label: string;
  /** "unknown" is a cannot-tell: enforcement refuses it, and the reason says what could not be read. */
  state: "allowed" | "partial" | "blocked" | "unknown";
  /** The sentence for a blocked or cannot-tell row; null when nothing is blocked. */
  reason: string | null;
  /** The deciding rule: why it is blocked, or why it is allowed. */
  rule: AccessRule | null;
  /** The model an alias reaches; empty for anything else. */
  points_to: { id: string; name: string }[];
  /** Set only when the entry is allowed for some kinds of work and not others, or the kinds differ. */
  kinds: AccessPreviewKind[];
}

export interface AccessPreviewCounts {
  total: number;
  usable: number;
  blocked: number;
  unknown: number;
}

export interface AccessPreview {
  plan_ids: string[];
  summary: AccessPreviewCounts;
  groups: ({ group: string; label: string } & AccessPreviewCounts)[];
  entries: AccessPreviewEntry[];
}

// ---------------------------------------------------------------------------
// Users surface

export interface AuditEntry {
  id: string;
  created_at: number;
  actor_kind: "admin" | "cli" | "service_account" | "system" | "user" | null;
  /** Server-resolved from the authenticated principal; never infer from actor_id. */
  actor_label: string;
  actor_id: string;
  action: string;
  target_kind: string | null;
  target_id: string | null;
  metadata: Record<string, unknown> | null;
}

/** Response shape for GET /admin/api/audit (keyset-paginated). */
export interface AuditPage {
  records: AuditEntry[];
  automated_count?: number;
  /** Opaque cursor "<created_at>:<id>"; null when no more rows exist for the
   * current filter set. Pass verbatim as the `before` query param. */
  next_cursor: string | null;
}

/** Filter + pagination options for the audit endpoint. */
export interface AuditQueryParams {
  limit?: number;
  actor_id?: string;
  action_prefix?: string;
  exclude_access_audit?: boolean;
  show_automated?: boolean;
  /** Keyset cursor from a prior next_cursor. */
  before?: string;
}

export interface StatusSeriesPoint extends Array<number> {
  0: number;
  1: number;
}

export interface BackendHistory {
  uptime_pct_24h: number | null;
  uptime_pct_7d: number | null;
  avg_latency_ms_24h: number | null;
  p95_latency_ms_24h: number | null;
  avg_latency_ms_7d: number | null;
  p95_latency_ms_7d: number | null;
  sample_count_24h: number;
  /** [timestamp_s, status_code] pairs; 1=up, 0.5=degraded, 0=down */
  status_series: StatusSeriesPoint[];
  /** [timestamp_s, latency_ms] pairs */
  latency_series: StatusSeriesPoint[];
}

export interface StatusPayload {
  banner?: "ok" | "warn" | "bad" | string;
  banner_reasons?: string[];
  system_health?: "ok" | "warn" | "bad" | string;
  system_health_reasons?: string[];
  generated_at?: number;
  current_health?: CurrentHealth;
  backends?: Array<{
    /** Legacy alias retained for backward compatibility; prefer service_id. */
    key?: string;
    /** Canonical identity, normalized server-side from key when identifiable. Absent for unidentifiable rows. */
    service_id?: string;
    name?: string;
    url?: string;
    role?: string;
    live_status?: string;
    status?: string;
    latency_ms?: number | null;
    last_error?: string | null;
    history?: BackendHistory;
  }>;
  auth_keys?: Array<{
    name?: string;
    app_id?: string | null;
    requests_24h?: number | null;
    errors_4xx_24h?: number | null;
    errors_5xx_24h?: number | null;
    latency_ms_p50?: number | null;
    latency_ms_p95?: number | null;
    last_seen?: number | null;
    active?: boolean | null;
  }>;
  metrics?: {
    available?: boolean;
    gpus?: Array<{
      index?: number;
      vram_pct_24h_series?: StatusSeriesPoint[];
      temp_24h_series?: StatusSeriesPoint[];
      vram_pct_max_24h?: number | null;
      temp_max_24h_c?: number | null;
    }>;
    cpu?: {
      load_avg_24h_series?: StatusSeriesPoint[];
      temp_max_24h_c?: number | null;
    };
    disk?: Record<
      string,
      {
        series_24h?: StatusSeriesPoint[];
        avail_change_24h_gb?: number | null;
        time_to_full_estimate_hours?: number | null;
      }
    >;
    trends?: {
      per_key_request_series_1h?: Record<string, StatusSeriesPoint[]>;
      per_key_rpm_5min?: Record<string, number | null>;
      xid_109_hourly_24h?: StatusSeriesPoint[];
      xid_109_24h_count?: number | null;
      xid_109_60min_count?: number | null;
    };
  };
  [key: string]: unknown;
}

export interface CurrentHealthService {
  engine_label?: string;
  capability_title?: string;
  capability_labels?: string[];
  icon_key?: import("./product-api-types").CapabilityIconKey;
  in_use?: boolean | null;
  /** The service record says it starts when needed. */
  starts_on_demand?: boolean;
  usage_label?: string | null;
  service_id: string;
  name: string;
  configured: boolean;
  enabled: boolean;
  role: string;
  routable: boolean | null;
  observation:
    | "up"
    | "degraded"
    | "down"
    | "standby_stopped"
    | "disabled"
    | "unavailable"
    | string;
  tone: "ok" | "warn" | "bad" | "neutral" | string;
  last_error: string | null;
}

export interface CurrentHealthSignal {
  source: string;
  tone: "warn" | "bad" | string;
  reasons: string[];
}

export interface CurrentHealthIncomplete {
  observations?: string[];
  routing?: string[];
}

/** The one current health projection shared by all admin read surfaces. */
export interface CurrentHealth {
  state: "ok" | "warn" | "bad" | "unavailable" | "empty" | "inactive" | string;
  tone: "ok" | "warn" | "bad" | "neutral" | string;
  label: string;
  detail: string;
  observation_available: boolean;
  /** Producer-authored API/host facts, distinct from per-service probes. */
  signals?: CurrentHealthSignal[];
  /** Explicit gaps in otherwise live coverage; never a replacement severity. */
  incomplete?: CurrentHealthIncomplete;
  services: CurrentHealthService[];
}

export interface StatusResponse {
  status: StatusPayload;
}

/** One item of the overview's attention list; the registry problem has kind "registry". */
export interface OverviewAttentionItem {
  kind: string;
  severity: string;
  title: string;
  detail?: string;
}

/** The part of GET /admin/api/overview the web app reads: why the running
 * config is not the stored registry, and the stored records left out. */
export interface OverviewPayload {
  attention: OverviewAttentionItem[];
  registry_apply: {
    mode: "partial" | "empty" | "stale";
    error: string;
    left_out: Record<string, string>;
    served: number;
  } | null;
  registry_excluded: Array<{ section: string; id: string; error: string }>;
}

// ---------------------------------------------------------------------------
// CSRF session bootstrap

const SESSION_PATH = "/admin/api/session";
const CSRF_REQUIRED_CODE = "admin_csrf_required";

let cachedSession: Session | null = null;
let sessionInFlight: Promise<Session> | null = null;

async function requestSession(): Promise<Session> {
  const response = await networkFetch(SESSION_PATH, {
    credentials: "same-origin",
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (isErrorEnvelope(payload)) {
      throw new ApiError(
        payload.error.code,
        payload.error.message,
        response.status,
        payload,
      );
    }
    throw new ApiError(
      "unexpected_response",
      `session request failed (${response.status})`,
      response.status,
    );
  }
  return payload as Session;
}

/** Fetch (and cache) the session: actor identity + CSRF token. */
export async function fetchSession(options?: {
  force?: boolean;
}): Promise<Session> {
  if (!options?.force && cachedSession) return cachedSession;
  if (!options?.force && sessionInFlight) return sessionInFlight;
  const request: Promise<Session> = requestSession()
    .then((session) => {
      if (sessionInFlight === request) cachedSession = session;
      return session;
    })
    .finally(() => {
      if (sessionInFlight === request) sessionInFlight = null;
    });
  sessionInFlight = request;
  return request;
}

/** Drop the cached session (sign-out, tests). */
export function resetSessionCache(): void {
  cachedSession = null;
  sessionInFlight = null;
}

// ---------------------------------------------------------------------------
// adminFetch

export type AdminFetchInit = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
};

async function rawFetch(
  path: string,
  init: AdminFetchInit,
  csrfToken: string | null,
): Promise<Response> {
  const method = init.method ?? "GET";
  const headers = new Headers();
  for (const [name, value] of Object.entries(init.headers ?? {})) {
    headers.set(name, value);
  }
  if (init.body !== undefined) {
    headers.set("content-type", "application/json");
  }
  if (method !== "GET") {
    headers.set("x-azizam-admin-action", "1");
    if (csrfToken) headers.set("x-azizam-csrf", csrfToken);
  }
  return networkFetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    signal: init.signal,
  });
}

/** The one fetch wrapper every admin call goes through. Reads need only the
 * cookie; writes get the action + CSRF headers injected. On a 403
 * `admin_csrf_required` the session is refetched and the write retried once
 * (the session rolled underneath us). Throws `ApiError` on any non-2xx. */
export async function adminFetch<T>(
  path: string,
  init: AdminFetchInit = {},
): Promise<T> {
  const method = init.method ?? "GET";
  const isWrite = method !== "GET";
  const isRegistryMutation = isServiceRegistryMutation(path, method);
  let requestInit = init;
  if (isRegistryMutation && !hasHeader(init.headers, "If-Match")) {
    if (!serviceRegistryEtag) {
      throw new ApiError(
        "generation_mismatch",
        "The service registry is not loaded yet. Refresh and try again (412 Precondition Failed).",
        412,
      );
    }
    requestInit = {
      ...init,
      headers: { ...(init.headers ?? {}), "If-Match": serviceRegistryEtag },
    };
  }

  let csrfToken: string | null = null;
  if (isWrite) {
    csrfToken = (await fetchSession()).actor.csrf_token || null;
  }

  let response = await rawFetch(path, requestInit, csrfToken);
  let payload: unknown =
    response.status === 204
      ? undefined
      : await response.json().catch(() => null);

  if (
    !response.ok &&
    response.status === 403 &&
    isWrite &&
    isErrorEnvelope(payload) &&
    payload.error.code === CSRF_REQUIRED_CODE
  ) {
    const fresh = await fetchSession({ force: true });
    response = await rawFetch(
      path,
      requestInit,
      fresh.actor.csrf_token || null,
    );
    payload =
      response.status === 204
        ? undefined
        : await response.json().catch(() => null);
  }

  if (!response.ok) {
    if (isErrorEnvelope(payload)) {
      throw new ApiError(
        payload.error.code,
        payload.error.message,
        response.status,
        payload,
      );
    }
    throw new ApiError(
      "unexpected_response",
      `${method} ${path} failed (${response.status})`,
      response.status,
    );
  }
  if (isServiceRegistryResponse(path, method)) {
    const etag = response.headers.get("etag");
    if (etag) serviceRegistryEtag = etag;
  }
  return payload as T;
}

/** Run a registry mutation against the latest known snapshot ETag. */
export async function adminRegistryMutation<T>(
  path: string,
  init: AdminFetchInit,
): Promise<T> {
  if (!serviceRegistryEtag) {
    await adminFetch<ServiceRegistryResponse>(SERVICE_REGISTRY_PATH);
  }
  try {
    return await adminFetch<T>(path, init);
  } catch (error) {
    if (error instanceof ApiError && error.status === 412) {
      // Refresh the retained ETag so the next operator retry has a current
      // precondition, while preserving the conflict for the UI to surface.
      await adminFetch<ServiceRegistryResponse>(SERVICE_REGISTRY_PATH).catch(
        () => undefined,
      );
    }
    throw error;
  }
}

// ---------------------------------------------------------------------------
// Typed endpoints (the Apps & Keys contract)

export const api = {
  session: () => fetchSession(),

  apps: {
    list: () =>
      adminFetch<{ apps: App[] }>("/admin/api/apps").then((r) => r.apps),
    /** Owner only: an access plan id, or "person" for the app's person's plan. */
    setAccessPlan: (appId: string, accessPlan: string) =>
      adminFetch<{ app: App }>(`/admin/api/apps/${encodeURIComponent(appId)}`, {
        method: "PATCH",
        body: { access_plan: accessPlan },
      }).then((r) => r.app),
  },
  users: {
    list: () =>
      adminFetch<{ users: User[] }>("/admin/api/users").then((r) => r.users),
  },

  accessPlans: {
    list: () =>
      adminFetch<{ access_plans: AccessPlan[] }>("/admin/api/access-plans").then(
        (r) => r.access_plans,
      ),
    create: (input: AccessPlanDocument) =>
      adminFetch<{ access_plan: AccessPlan }>("/admin/api/access-plans", {
        method: "POST",
        body: input,
      }).then((r) => r.access_plan),
    update: (planId: string, input: AccessPlanDocument) =>
      adminFetch<{ access_plan: AccessPlan }>(
        `/admin/api/access-plans/${encodeURIComponent(planId)}`,
        { method: "PUT", body: input },
      ).then((r) => r.access_plan),
    delete: (planId: string) =>
      adminFetch<{ access_plan: AccessPlan }>(
        `/admin/api/access-plans/${encodeURIComponent(planId)}`,
        { method: "DELETE" },
      ).then((r) => r.access_plan),
    editor: () =>
      adminFetch<AccessPlanEditorContract>("/admin/api/access-plans/editor"),
    /** Everything the plan can and cannot use. Makes no model request. */
    preview: (planId: string) =>
      adminFetch<AccessPreview>("/admin/api/access/preview", {
        method: "POST",
        body: { plan: planId },
      }),
  },

  services: {
    registry: () =>
      adminFetch<ServiceRegistryResponse>(
        "/admin/api/operator/service-registry",
      ).then((r) => r.service_registry),
    edit: (serviceId: string, input: Record<string, unknown>) =>
      adminRegistryMutation<ServiceRegistryResponse>(
        `/admin/api/operator/services/${serviceId}`,
        { method: "PATCH", body: input },
      ).then((r) => r.service_registry),
    remove: (serviceId: string) =>
      adminRegistryMutation<ServiceRegistryResponse>(
        `/admin/api/operator/services/${serviceId}`,
        { method: "DELETE" },
      ).then((r) => r.service_registry),
  },

  status: () =>
    adminFetch<StatusResponse>("/admin/api/status").then((r) => r.status),

  overview: () => adminFetch<OverviewPayload>("/admin/api/overview"),

  config: {
    /** Apply the stored registry to the running gateway again. */
    reload: () =>
      adminFetch<{ reloaded: boolean }>("/admin/api/config/reload", {
        method: "POST",
      }),
  },

  audit: {
    /** Paginated fetch. Returns {records, next_cursor}. */
    list: (params: AuditQueryParams = {}) => {
      const q = new URLSearchParams();
      if (params.limit !== undefined) q.set("limit", String(params.limit));
      if (params.actor_id) q.set("actor_id", params.actor_id);
      if (params.action_prefix) q.set("action_prefix", params.action_prefix);
      if (params.exclude_access_audit !== undefined)
        q.set("exclude_access_audit", String(params.exclude_access_audit));
      if (params.show_automated !== undefined)
        q.set("show_automated", String(params.show_automated));
      if (params.before) q.set("before", params.before);
      const qs = q.toString();
      return adminFetch<AuditPage>(`/admin/api/audit${qs ? `?${qs}` : ""}`);
    },
  },
};

// ---------------------------------------------------------------------------
// React Query

/** Stable query keys; surfaces share cache entries through these. */
export const queryKeys = {
  session: ["session"] as const,
  apps: ["apps"] as const,
  users: ["users"] as const,
  credentials: ["credentials"] as const,
  accessPlans: ["access-plans"] as const,
  overview: ["overview"] as const,
  /** Recent audit teaser (3 rows on the overview). */
  auditRecent: ["audit", "recent"] as const,
  /** Parameterized audit key — includes filters so each filter combo is cached
   * independently. The cursor is NOT part of the key: pages are accumulated in
   * component state, not in separate cache entries. */
  audit: (params: { actorId: string | null; actionPrefix: string | null }) =>
    ["audit", params] as const,
  auditEvent: (eventId: string | null) => ["audit", "event", eventId] as const,
  serviceRegistry: ["serviceRegistry"] as const,
  status: ["status"] as const,
  litellmConfig: ["litellmConfig"] as const,
  operatorOverrides: ["operator", "overrides"] as const,
  accessPlanEditor: ["access-plan-editor"] as const,
  accessPreview: (planId: string) => ["access-preview", planId] as const,
};

/** QueryClient with contract-aware retry: 4xx errors are deterministic
 * (validation/permissions), so only network/5xx failures retry. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Reuse fresh reads across navigation and focus; polling still controls
        // health and active-job freshness independently of this cache window.
        staleTime: 30_000,
        refetchIntervalInBackground: false,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status < 500) return false;
          return failureCount < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}
