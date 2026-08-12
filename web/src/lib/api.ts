/** Typed API client for the gateway JSON contract
 * (docs/design/web-api-contract.md). One `adminFetch` injects the CSRF
 * headers on writes; every non-2xx response becomes a typed `ApiError`
 * parsed from the `{"error":{code,message}}` envelope.
 *
 * Auth model: same-origin session cookie. Writes additionally require
 *   x-ai-gateway-admin-action: 1
 *   x-ai-gateway-csrf: <token from GET /admin/api/session>
 * The CSRF token is fetched lazily, cached, and refreshed once on a
 * 403 admin_csrf_required (rolled session). No secret values are ever
 * logged by this module. */

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

export interface ApiErrorEnvelope {
  error: { code: string; message: string };
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
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
  subject: string;
  email: string | null;
  roles: string[];
  /** Empty string for bearer-key callers (no CSRF needed there). */
  csrf_token: string;
}

export interface Session {
  actor: SessionActor;
}

export type AppKind = "service" | "personal";

export interface App {
  id: string;
  kind: AppKind;
  name: string;
  owner_user_id: string | null;
  owner_service_account_id: string | null;
  default_profile_id: string | null;
  created_at: number;
  disabled_at: number | null;
  effective_profile_id: string;
  effective_profile_name: string;
  effective_profile_source: "app_default" | "fallback";
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

export type CredentialSubjectKind = "app" | "service_account" | "user";

export interface Credential {
  id: string;
  subject_kind: CredentialSubjectKind;
  subject_id: string;
  app_id: string | null;
  profile_id: string | null;
  /** First 18 chars of the full key — render via <SecretMask> only. */
  prefix: string;
  display_name: string;
  scopes: string[];
  created_at: number;
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
}

export interface Profile {
  id: string;
  name: string;
  description: string | null;
  created_at: number;
  profile: Record<string, unknown>;
  protocols: string[];
  workflows: string[];
  status_visibility: string;
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

export interface ProviderPreset {
  id: string;
  label: string;
  base_path: string;
  default_port: number | null;
  protocol: string | null;
  capabilities: string[];
  native_passthrough: boolean;
}

export interface CapabilityChoice {
  id: string;
  label: string;
}

export interface PresetsResponse {
  presets: ProviderPreset[];
  /** Canonical capability choices supplied by the server. */
  capabilities?: CapabilityChoice[];
}

// ---------------------------------------------------------------------------
// Profiles (full CRUD types)

export interface CreateProfileInput {
  name: string;
  description?: string;
  profile: {
    id?: string;
    protocols?: string[];
    status_visibility?: { level: string };
    allow?: {
      intents?: string[];
      models?: string[];
      workflows?: string[];
    };
  };
}

export interface UpdateProfileInput {
  name?: string;
  description?: string;
  profile?: {
    protocols?: string[];
    status_visibility?: { level: string };
    allow?: {
      intents?: string[];
      models?: string[];
      workflows?: string[];
    };
  };
}

export interface SimulateRequest {
  profile: string;
  request: {
    protocol?: string | null;
    intent?: string | null;
    model?: string | null;
    workflow?: string | null;
    backend?: string | null;
    capability_exposure?: string;
    capability_trust_tier?: string;
    capability_resource_tier?: string;
    capability_tags?: string[];
    requires_cloud_fallback?: boolean;
    native_passthrough?: boolean;
    native_operation?: string | null;
    native_backend?: string | null;
  };
}

export interface SimulateResult {
  profile_id: string;
  decision: {
    decision: "allowed" | "denied";
    allowed: boolean;
    sources: string[];
    reasons: string[];
    visibility_level: string;
  };
}

// ---------------------------------------------------------------------------
// Users surface

export interface UserAccessDefaults {
  self_service_apps: boolean;
  auto_create_personal_app: boolean;
}

export type RuntimeMappingSourceKind =
  | "oidc_group"
  | "oidc_role"
  | "email"
  | "domain"
  | "gateway_role"
  | "all_users";

export interface RuntimeProfileMapping {
  id: string;
  source_kind: RuntimeMappingSourceKind;
  source_value: string;
  profile_id: string;
  created_at: number;
  created_by: string;
  disabled_at: number | null;
}

export interface AttentionItem {
  kind: string;
  severity: "problem" | "attention";
  title: string;
  detail: string;
  /** Compatibility-only. The client derives links from the canonical tuple. */
  href?: string;
  target_kind?: string | null;
  target_id?: string | null;
  /** Display only; never use this value to resolve a route. */
  target_name?: string | null;
}

export interface AuditEntry {
  id: string;
  created_at: number;
  actor_kind: "admin" | "cli" | "service_account" | "user" | null;
  actor_id: string;
  action: string;
  target_kind: string | null;
  target_id: string | null;
  metadata: Record<string, unknown> | null;
}

/** Response shape for GET /admin/api/audit (keyset-paginated). */
export interface AuditPage {
  records: AuditEntry[];
  /** Opaque cursor "<created_at>:<id>"; null when no more rows exist for the
   * current filter set. Pass verbatim as the `before` query param. */
  next_cursor: string | null;
}

/** Filter + pagination options for the audit endpoint. */
export interface AuditQueryParams {
  limit?: number;
  actor_id?: string;
  action_prefix?: string;
  /** Keyset cursor from a prior next_cursor. */
  before?: string;
}

export interface Overview {
  actor: Omit<SessionActor, "csrf_token">;
  counts: {
    users: number;
    apps: number;
    profiles: number;
    runtime_profile_mappings: number;
    credentials: number;
    active_credentials: number;
    services_total?: number;
    services_enabled?: number;
    /** Non-human control-plane principals; distinct from people/users. */
    service_principals?: number;
  };
  attention: AttentionItem[];
  current_health: CurrentHealth;
  runtime_profile_mappings: Record<string, string>;
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
    key?: string;
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

// ---------------------------------------------------------------------------
// CSRF session bootstrap

const SESSION_PATH = "/admin/api/session";
const CSRF_REQUIRED_CODE = "admin_csrf_required";

let cachedSession: Session | null = null;
let sessionInFlight: Promise<Session> | null = null;

async function requestSession(): Promise<Session> {
  const response = await fetch(SESSION_PATH, { credentials: "same-origin" });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (isErrorEnvelope(payload)) {
      throw new ApiError(
        payload.error.code,
        payload.error.message,
        response.status,
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
  sessionInFlight = requestSession()
    .then((session) => {
      cachedSession = session;
      return session;
    })
    .finally(() => {
      sessionInFlight = null;
    });
  return sessionInFlight;
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
    headers.set("x-ai-gateway-admin-action", "1");
    if (csrfToken) headers.set("x-ai-gateway-csrf", csrfToken);
  }
  return fetch(path, {
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

export interface CreateAppInput {
  name: string;
  kind: AppKind;
  profile_id?: string;
  owner_user_id?: string;
}

export interface UpdateAppInput {
  name?: string;
  default_profile_id?: string;
}

export interface MintKeyInput {
  label?: string;
  profile_id?: string;
  scopes?: string[];
}

export interface CreateAdminKeyInput {
  name: string;
  role: "admin" | "owner";
}

export const api = {
  session: () => fetchSession(),

  apps: {
    list: () =>
      adminFetch<{ apps: App[] }>("/admin/api/apps").then((r) => r.apps),
    create: (input: CreateAppInput) =>
      adminFetch<{ app: App }>("/admin/api/apps", {
        method: "POST",
        body: input,
      }).then((r) => r.app),
    update: (appId: string, input: UpdateAppInput) =>
      adminFetch<{ app: App }>(`/admin/api/apps/${appId}`, {
        method: "PATCH",
        body: input,
      }).then((r) => r.app),
    /** DELETE disables (soft); the contract has no hard delete. */
    disable: (appId: string) =>
      adminFetch<{ app: App }>(`/admin/api/apps/${appId}`, {
        method: "DELETE",
      }).then((r) => r.app),
    /** POST /admin/api/apps/{id}/enable clears disabled_at (undo for disable). */
    enable: (appId: string) =>
      adminFetch<{ app: App }>(`/admin/api/apps/${appId}/enable`, {
        method: "POST",
      }).then((r) => r.app),
    mintKey: (appId: string, input: MintKeyInput = {}) =>
      adminFetch<{ credential: MintedCredential }>(
        `/admin/api/apps/${appId}/keys`,
        { method: "POST", body: input },
      ).then((r) => r.credential),
  },

  users: {
    list: () =>
      adminFetch<{ users: User[] }>("/admin/api/users").then((r) => r.users),
  },

  credentials: {
    list: () =>
      adminFetch<{ credentials: Credential[] }>("/admin/api/credentials").then(
        (r) => r.credentials,
      ),
    revoke: (credentialId: string) =>
      adminFetch<{ credential: Credential }>(
        `/admin/api/credentials/${credentialId}`,
        { method: "DELETE" },
      ).then((r) => r.credential),
  },

  adminKeys: {
    create: (input: CreateAdminKeyInput) =>
      adminFetch<{ credential: MintedCredential }>("/admin/api/admin-keys", {
        method: "POST",
        body: input,
      }).then((r) => r.credential),
  },

  profiles: {
    list: () =>
      adminFetch<{ profiles: Profile[] }>("/admin/api/profiles").then(
        (r) => r.profiles,
      ),
    create: (input: CreateProfileInput) =>
      adminFetch<{ profile: Profile }>("/admin/api/profiles", {
        method: "POST",
        body: input,
      }).then((r) => r.profile),
    update: (profileId: string, input: UpdateProfileInput) =>
      adminFetch<{ profile: Profile }>(`/admin/api/profiles/${profileId}`, {
        method: "PATCH",
        body: input,
      }).then((r) => r.profile),
    delete: (profileId: string) =>
      adminFetch<{ profile: Profile }>(`/admin/api/profiles/${profileId}`, {
        method: "DELETE",
      }).then((r) => r.profile),
  },

  policy: {
    simulate: (input: SimulateRequest) =>
      adminFetch<SimulateResult>("/admin/api/policy/simulate", {
        method: "POST",
        body: input,
      }),
  },

  userAccessDefaults: {
    get: () =>
      adminFetch<{ overrides: { user_access_defaults: UserAccessDefaults } }>(
        "/admin/api/operator/overrides",
      ).then((r) => r.overrides.user_access_defaults),
    update: (input: Partial<UserAccessDefaults>) =>
      adminFetch<{ user_access_defaults: UserAccessDefaults }>(
        "/admin/api/operator/user-access-defaults",
        { method: "PUT", body: input },
      ).then((r) => r.user_access_defaults),
  },

  runtimeProfileMappings: {
    list: () =>
      adminFetch<{ mappings: RuntimeProfileMapping[] }>(
        "/admin/api/runtime-profile-mappings",
      ).then((r) => r.mappings),
    create: (input: {
      source_kind: RuntimeMappingSourceKind;
      source_value: string;
      profile_id: string;
    }) =>
      adminFetch<{ mapping: RuntimeProfileMapping }>(
        "/admin/api/runtime-profile-mappings",
        { method: "POST", body: input },
      ).then((r) => r.mapping),
    disable: (mappingId: string) =>
      adminFetch<{ mapping: RuntimeProfileMapping }>(
        `/admin/api/runtime-profile-mappings/${mappingId}`,
        { method: "DELETE" },
      ).then((r) => r.mapping),
  },

  services: {
    registry: () =>
      adminFetch<ServiceRegistryResponse>(
        "/admin/api/operator/service-registry",
      ).then((r) => r.service_registry),
    presets: () =>
      adminFetch<PresetsResponse>("/admin/api/operator/service-presets"),
    add: (input: Record<string, unknown>) =>
      adminRegistryMutation<ServiceRegistryResponse>(
        "/admin/api/operator/services",
        {
          method: "POST",
          body: input,
        },
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
    toggleEnabled: (serviceId: string, enabled: boolean) =>
      adminRegistryMutation<ServiceRegistryResponse>(
        `/admin/api/operator/services/${serviceId}/enabled`,
        { method: "POST", body: { enabled } },
      ).then((r) => r.service_registry),
    testConnection: (input: Record<string, unknown>) =>
      adminFetch<{
        probe: {
          ok: boolean;
          status: number | null;
          error: string;
          url: string;
        };
      }>("/admin/api/operator/services/test", {
        method: "POST",
        body: input,
      }).then((r) => r.probe),
    detectModels: (input: Record<string, unknown>) =>
      adminFetch<{
        detected: { ok: boolean; models: string[]; error: string };
      }>("/admin/api/operator/services/detect-models", {
        method: "POST",
        body: input,
      }).then((r) => r.detected),
    litellmConfig: () =>
      adminFetch<{
        litellm_config: {
          model_list: Array<{
            model_name: string;
            litellm_params: Record<string, unknown>;
          }>;
          general_settings?: Record<string, unknown>;
        };
      }>("/admin/api/operator/litellm-config").then((r) => r.litellm_config),
  },

  overview: () => adminFetch<Overview>("/admin/api/overview"),
  status: () =>
    adminFetch<StatusResponse>("/admin/api/status").then((r) => r.status),

  audit: {
    /** Paginated fetch. Returns {records, next_cursor}. */
    list: (params: AuditQueryParams = {}) => {
      const q = new URLSearchParams();
      if (params.limit !== undefined) q.set("limit", String(params.limit));
      if (params.actor_id) q.set("actor_id", params.actor_id);
      if (params.action_prefix) q.set("action_prefix", params.action_prefix);
      if (params.before) q.set("before", params.before);
      const qs = q.toString();
      return adminFetch<AuditPage>(`/admin/api/audit${qs ? `?${qs}` : ""}`);
    },
    get: (eventId: string) =>
      adminFetch<{ event: AuditEntry }>(
        `/admin/api/audit/${encodeURIComponent(eventId)}`,
      ).then((r) => r.event),
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
  profiles: ["profiles"] as const,
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
  servicePresets: ["servicePresets"] as const,
  status: ["status"] as const,
  litellmConfig: ["litellmConfig"] as const,
  operatorOverrides: ["operator", "overrides"] as const,
  runtimeProfileMappings: ["runtime-profile-mappings"] as const,
};

/** QueryClient with contract-aware retry: 4xx errors are deterministic
 * (validation/permissions), so only network/5xx failures retry. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status < 500) return false;
          return failureCount < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}
