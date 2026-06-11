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

export interface AttentionItem {
  kind: string;
  severity: "problem" | "attention";
  title: string;
  detail: string;
  section: string;
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
  };
  attention: AttentionItem[];
  runtime_profile_mappings: Record<string, string>;
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
};

async function rawFetch(
  path: string,
  init: AdminFetchInit,
  csrfToken: string | null,
): Promise<Response> {
  const method = init.method ?? "GET";
  const headers = new Headers();
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

  let csrfToken: string | null = null;
  if (isWrite) {
    csrfToken = (await fetchSession()).actor.csrf_token || null;
  }

  let response = await rawFetch(path, init, csrfToken);
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
    response = await rawFetch(path, init, fresh.actor.csrf_token || null);
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
  return payload as T;
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
  },

  overview: () => adminFetch<Overview>("/admin/api/overview"),
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
