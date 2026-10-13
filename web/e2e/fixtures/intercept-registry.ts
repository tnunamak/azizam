// Endpoints a browser test may stub with fulfilJson (intercept.ts). One entry
// per route family: the methods and path it covers, and where the real
// structure comes from:
//   probe   - a read-only call on the fixture gateway the stub's server runs,
//             made with the lane owner's session;
//   oracle  - a call that changes state or needs data the seed lacks. oracle.ts
//             makes it once per run on a gateway of its own and writes the body;
//   unprobed - no structure can be taken (the reason says why). Its stubs are
//             only checked to be JSON. None is left; policy.py fails any new one.
// The real body gives the structure a stub must have.
//
// required: paths of keys a stub must carry, "[]" for any array index. A stub is
//   a slice of the real response, so only the keys the product reads are listed.
// open: paths of objects whose keys are data (ids, model names), not fields.
import type { APIRequestContext } from "@playwright/test";

export interface Endpoint {
  id: string;
  methods: string[];
  path: RegExp;
  probe?: (api: APIRequestContext) => Promise<unknown>;
  // The call is made once per run on the oracle gateway (oracle.ts); the body it
  // returns gives the structure, as a probe's body does.
  oracle?: true;
  unprobed?: string;
  open?: string[];
  free?: string[];
  required?: string[];
}

async function read(api: APIRequestContext, path: string): Promise<unknown> {
  const response = await api.get(path);
  if (response.status() !== 200)
    throw new Error(
      `intercept probe GET ${path} returned HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`,
    );
  return response.json();
}

// Keys a stub of each endpoint must carry: those every stub in the suite carried when this list was
// made (2026-10-12) that the real response also has. Edit by hand; intercept.ts fails when a listed
// key is gone from the real response.
const REQUIRED: Record<string, string[]> = {
  "/admin/api/activity/requests": [
    "/next_cursor",
    "/people",
    "/records",
    "/records/[]/app_label",
    "/records/[]/created_at",
    "/records/[]/person_label",
    "/records/[]/receipt",
    "/records/[]/receipt/intent",
    "/records/[]/receipt/latency_ms",
    "/records/[]/receipt/request_id",
    "/records/[]/receipt/requested_model",
    "/records/[]/receipt/resolved_model",
    "/records/[]/receipt/status",
  ],
  "/admin/api/categories/history": ["/history"],
  "/admin/api/categories/services": [
    "/services",
    "/services/[]/capabilities",
    "/services/[]/kind",
    "/services/[]/label",
    "/services/[]/stem",
  ],
  "/admin/api/extensions": [
    "/extension_packages",
    "/extension_packages/packages",
  ],
  "/admin/api/invites": ["/invites", "/profiles"],
  "/admin/api/oidc-config": ["/oidc_config", "/revision"],
  "/admin/api/operator/overrides": [
    "/overrides",
    "/overrides/media_job_admission",
    "/overrides/revisions",
    "/overrides/revisions/media_job_admission_overrides",
    "/overrides/user_access_defaults",
    "/overrides/user_access_defaults/auto_create_personal_app",
    "/overrides/user_access_defaults/self_service_apps",
  ],
  "/admin/api/operator/service-registry": [
    "/service_registry",
    "/service_registry/schema_version",
    "/service_registry/services",
  ],
  "/admin/api/operator/sign-in": [
    "/built_in",
    "/mode",
    "/oidc",
    "/oidc_label",
    "/revision",
  ],
  "/admin/api/people": ["/people", "/profiles"],
  "/admin/api/policy/editor": [
    "/profile",
    "/profile/choices",
    "/profile/choices/exposure",
    "/profile/choices/exposure/[]/id",
    "/profile/choices/exposure/[]/label",
    "/profile/choices/intents",
    "/profile/choices/intents/[]/id",
    "/profile/choices/intents/[]/label",
    "/profile/choices/models",
    "/profile/choices/models/[]/id",
    "/profile/choices/models/[]/label",
    "/profile/choices/protocols",
    "/profile/choices/protocols/[]/id",
    "/profile/choices/protocols/[]/label",
    "/profile/choices/resource_tiers",
    "/profile/choices/status_visibility",
    "/profile/choices/tags",
    "/profile/choices/target_activation",
    "/profile/choices/target_visibility",
    "/profile/choices/trust_tiers",
    "/profile/choices/workflows",
    "/profile/create",
    "/profile/create/profile",
    "/profile/set_semantics",
    "/profile/set_semantics/empty",
    "/profile/set_semantics/omitted",
    "/schema_version",
  ],
  "/admin/api/providers": ["/providers"],
  "/admin/api/providers/-/catalog": [
    "/providers",
    "/providers/[]/base_url",
    "/providers/[]/billing_dimensions",
    "/providers/[]/display_name",
    "/providers/[]/id",
    "/providers/[]/logo_key",
    "/providers/[]/protocol",
  ],
  "/admin/api/providers/openrouter/reconciliation": ["/reconciliations"],
  "/admin/api/settings/defaults": [
    "/aliases",
    "/defaults",
    "/defaults/profile_id",
  ],
  "/admin/api/settings/effective": [
    "/config",
    "/config/proxy",
    "/config/proxy/port",
    "/export_url",
  ],
  "/admin/api/settings/instance": ["/instance", "/instance/base_url"],
  "/admin/api/settings/prompt-memory": [
    "/prompt_memory",
    "/prompt_memory/default_remember",
    "/prompt_memory/never_keep",
    "/prompt_memory/retention_cap",
    "/revision",
  ],
  "/admin/api/settings/receipts": ["/retention_days", "/revision"],
  "/admin/api/status": ["/status"],
  "/admin/api/users": [
    "/users",
    "/users/[]/display_name",
    "/users/[]/email",
    "/users/[]/id",
    "/users/[]/name",
  ],
  "/auth/api/options": ["/built_in", "/oidc", "/oidc_label", "/oidc_url"],
  "/health": ["/status"],
  "/me/api/home": [
    "/aliases",
    "/authenticated",
    "/capabilities",
    "/receipts",
    "/user",
    "/user/email",
    "/user/id",
    "/user/name",
    "/user/role",
  ],
  "/me/api/jobs": ["/jobs", "/next_cursor"],
  "/me/api/media": ["/assets"],
  "/me/api/models/catalog": ["/aliases", "/models"],
  "/me/api/session": [
    "/authenticated",
    "/user",
    "/user/email",
    "/user/id",
    "/user/name",
    "/user/role",
  ],
  "/me/api/status": [
    "/capabilities",
    "/help",
    "/receipts",
    "/scope",
    "/status",
    "/status/state",
  ],
  "/me/api/usage/summary": [
    "/by_day",
    "/by_model",
    "/media_count",
    "/receipts",
    "/remote_spend",
    "/remote_spend_available",
    "/requests",
    "/retention_days",
  ],
  "/me/api/workflows": ["/workflows"],
  "/setup/api/state": ["/owner_exists"],
  "/showcase/examples/index.json": ["/data", "/object"],
  "/showcase/static/sections.json": [
    "/sections",
    "/sections/[]/fallback",
    "/sections/[]/id",
    "/sections/[]/match",
    "/sections/[]/match/intent",
    "/sections/[]/source",
    "/sections/[]/title",
  ],
};

const get = (path: string) => (api: APIRequestContext) => read(api, path);
const exact = (path: string) =>
  new RegExp(`^${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

type Spec = Omit<Endpoint, "id" | "path" | "methods"> & {
  methods?: string[];
  path?: RegExp;
};

// Admin and member reads that the lane owner's session can make as they are.
const reads = (path: string, extra: Spec = {}): Endpoint => ({
  id: `GET ${path}`,
  methods: ["GET"],
  path: exact(path),
  probe: get(path),
  required: REQUIRED[path],
  ...extra,
});

// A call that changes state or needs data the browser seed lacks: oracle.ts makes it
// once on a gateway of its own and the response gives the structure.
const executed = (
  method: string,
  path: string,
  extra: Spec = {},
): Endpoint => ({
  id: `${method} ${path}`,
  methods: [method],
  path: exact(path),
  oracle: true,
  ...extra,
});

// The escape hatch for an endpoint with no structural oracle. scripts/ci/policy.py
// counts its call sites and tests/budgets.toml sets that ceiling to 0.
export const unprobed = (
  method: string,
  path: string,
  reason: string,
  pattern?: RegExp,
): Endpoint => ({
  id: `${method} ${path}`,
  methods: [method],
  path: pattern ?? exact(path),
  unprobed: reason,
});

export const REGISTRY: Endpoint[] = [
  reads("/.well-known/azizam"),
  reads("/auth/api/options", {
    required: ["/built_in", "/oidc", "/oidc_label", "/oidc_url"],
  }),
  reads("/setup/api/state", { required: ["/owner_exists"] }),
  reads("/health", { required: ["/status"] }),
  reads("/showcase/static/sections.json", {
    required: [
      "/sections",
      "/sections/[]/id",
      "/sections/[]/match/intent",
      "/sections/[]/title",
    ],
  }),
  reads("/showcase/examples/index.json", { required: ["/data", "/object"] }),
  reads("/admin/api/activity/requests", {
    required: [
      "/next_cursor",
      "/people",
      "/records",
      "/records/[]/app_label",
      "/records/[]/created_at",
      "/records/[]/person_label",
      "/records/[]/receipt",
      "/records/[]/receipt/intent",
      "/records/[]/receipt/latency_ms",
      "/records/[]/receipt/request_id",
      "/records/[]/receipt/requested_model",
      "/records/[]/receipt/resolved_model",
      "/records/[]/receipt/status",
    ],
  }),
  reads("/admin/api/audit"),
  {
    id: "GET /admin/api/categories/active",
    methods: ["GET"],
    path: exact("/admin/api/categories/active"),
    oracle: true,
    free: ["/snapshot"],
  },
  reads("/admin/api/categories/history", { required: ["/history"] }),
  reads("/admin/api/categories/services", {
    required: [
      "/services",
      "/services/[]/capabilities",
      "/services/[]/kind",
      "/services/[]/label",
      "/services/[]/stem",
    ],
  }),
  reads("/admin/api/extensions", {
    required: ["/extension_packages", "/extension_packages/packages"],
    open: ["/extension_packages/packages"],
  }),
  reads("/admin/api/invites", {
    required: ["/invites", "/profiles"],
    free: ["/profiles/[]/profile"],
  }),
  reads("/admin/api/oidc-config", { required: ["/oidc_config", "/revision"] }),
  reads("/admin/api/operator/overrides", {
    required: [
      "/overrides",
      "/overrides/media_job_admission",
      "/overrides/revisions",
      "/overrides/revisions/media_job_admission_overrides",
      "/overrides/user_access_defaults",
      "/overrides/user_access_defaults/auto_create_personal_app",
      "/overrides/user_access_defaults/self_service_apps",
    ],
  }),
  reads("/admin/api/operator/service-registry", {
    required: [
      "/service_registry",
      "/service_registry/schema_version",
      "/service_registry/services",
    ],
    open: ["/service_registry/services"],
    free: ["/service_registry/services/*/config"],
  }),
  reads("/admin/api/operator/sign-in", {
    required: ["/built_in", "/mode", "/oidc", "/oidc_label", "/revision"],
  }),
  reads("/admin/api/people", {
    required: ["/people", "/profiles"],
    free: ["/profiles/[]/profile"],
  }),
  reads("/admin/api/profiles", { free: ["/profiles/[]/profile"] }),
  reads("/admin/api/policy/editor", {
    required: [
      "/profile",
      "/profile/choices",
      "/profile/choices/exposure",
      "/profile/choices/exposure/[]/id",
      "/profile/choices/exposure/[]/label",
      "/profile/choices/intents",
      "/profile/choices/intents/[]/id",
      "/profile/choices/intents/[]/label",
      "/profile/choices/models",
      "/profile/choices/models/[]/id",
      "/profile/choices/models/[]/label",
      "/profile/choices/protocols",
      "/profile/choices/protocols/[]/id",
      "/profile/choices/protocols/[]/label",
      "/profile/choices/resource_tiers",
      "/profile/choices/status_visibility",
      "/profile/choices/tags",
      "/profile/choices/target_activation",
      "/profile/choices/target_visibility",
      "/profile/choices/trust_tiers",
      "/profile/choices/workflows",
      "/profile/create",
      "/profile/create/profile",
      "/profile/set_semantics",
      "/profile/set_semantics/empty",
      "/profile/set_semantics/omitted",
      "/schema_version",
    ],
  }),
  reads("/admin/api/providers", { required: ["/providers"] }),
  reads("/admin/api/providers/-/catalog", {
    required: [
      "/providers",
      "/providers/[]/base_url",
      "/providers/[]/billing_dimensions",
      "/providers/[]/display_name",
      "/providers/[]/id",
      "/providers/[]/logo_key",
      "/providers/[]/protocol",
    ],
  }),
  reads("/admin/api/providers/openrouter/reconciliation", {
    required: ["/reconciliations"],
  }),
  reads("/admin/api/settings/defaults", {
    required: ["/aliases", "/defaults", "/defaults/profile_id"],
  }),
  reads("/admin/api/settings/effective", {
    required: ["/config", "/export_url"],
    free: ["/config"],
  }),
  reads("/admin/api/settings/instance", {
    required: ["/instance", "/instance/base_url"],
  }),
  reads("/admin/api/settings/prompt-memory", {
    required: [
      "/prompt_memory",
      "/prompt_memory/default_remember",
      "/prompt_memory/never_keep",
      "/prompt_memory/retention_cap",
      "/revision",
    ],
  }),
  reads("/admin/api/settings/receipts", {
    required: ["/retention_days", "/revision"],
  }),
  reads("/admin/api/status", { required: ["/status"] }),
  reads("/admin/api/users", {
    required: [
      "/users",
      "/users/[]/display_name",
      "/users/[]/email",
      "/users/[]/id",
      "/users/[]/name",
    ],
  }),
  reads("/me/api/home", {
    open: ["/credential_aliases"],
    required: [
      "/aliases",
      "/authenticated",
      "/capabilities",
      "/receipts",
      "/user",
      "/user/email",
      "/user/id",
      "/user/name",
      "/user/role",
    ],
  }),
  reads("/me/api/jobs", { required: ["/jobs", "/next_cursor"] }),
  reads("/me/api/media", { required: ["/assets"] }),
  reads("/me/api/models/catalog", { required: ["/aliases", "/models"] }),
  reads("/me/api/session", {
    required: [
      "/authenticated",
      "/user",
      "/user/email",
      "/user/id",
      "/user/name",
      "/user/role",
    ],
  }),
  reads("/me/api/status", {
    required: [
      "/capabilities",
      "/help",
      "/receipts",
      "/scope",
      "/status",
      "/status/state",
    ],
  }),
  reads("/me/api/usage/summary", {
    required: [
      "/by_day",
      "/by_model",
      "/media_count",
      "/receipts",
      "/remote_spend",
      "/remote_spend_available",
      "/requests",
      "/retention_days",
    ],
  }),
  reads("/me/api/workflows", {
    required: ["/workflows"],
    free: [
      "/workflows/[]/example_request/body",
      "/workflows/[]/example_request/form",
      "/workflows/[]/example_request/files",
      "/workflows/[]/openapi/execution/sync/fields",
    ],
  }),
  {
    id: "GET /me/api/media/{id}",
    methods: ["GET"],
    path: /^\/me\/api\/media\/[^/]+$/,
    oracle: true,
  },
  executed("GET", "/me/api/media/{id}/download", { path: /^\/me\/api\/media\/[^/]+\/download$/ }),
  executed("POST", "/v1/systemone"),
  executed("POST", "/openai/v1/embeddings"),
  executed("DELETE", "/me/api/media/{id}", { path: /^\/me\/api\/media\/[^/]+$/ }),
  executed("GET", "/me/api/receipts/{id}", { path: /^\/me\/api\/receipts\/[^/]+$/ }),
  executed("GET", "/admin/api/invites/{id}/link", {
    path: /^\/admin\/api\/invites\/[^/]+\/link$/,
    required: ["/url"],
  }),
  executed("GET", "/gateway/v1/media/jobs/{id}", {
    path: /^\/gateway\/v1\/media\/jobs\/[^/]+$/,
    required: ["/status"],
  }),
  executed("GET", "/gateway/v1/media/jobs/{id}/content", { path: /^\/gateway\/v1\/media\/jobs\/[^/]+\/content$/ }),
  executed("GET", "/v1/videos/{id}", { path: /^\/v1\/videos\/[^/]+$/ }),
  executed("DELETE", "/admin/api/invites/{id}", { path: /^\/admin\/api\/invites\/[^/]+$/ }),
  executed("DELETE", "/admin/api/people/{id}", { path: /^\/admin\/api\/people\/[^/]+$/ }),
  executed("PATCH", "/admin/api/operator/services/{id}", {
    path: /^\/admin\/api\/operator\/services\/[^/]+$/,
    open: ["/service_registry/services"],
    free: ["/service_registry/services/*/config"],
  }),
  executed("PATCH", "/admin/api/people/{id}", {
    path: /^\/admin\/api\/people\/[^/]+$/,
    required: ["/ok"],
  }),
  executed("PATCH", "/admin/api/profiles/{id}", {
    path: /^\/admin\/api\/profiles\/[^/]+$/,
    free: ["/profile/profile"],
  }),
  executed("POST", "/admin/api/invites", {
    required: ["/invite", "/invite/id", "/url"],
  }),
  executed("POST", "/admin/api/operator/services/test"),
  executed("POST", "/admin/api/people/{id}/reset", { path: /^\/admin\/api\/people\/[^/]+\/reset$/ }),
  executed("POST", "/admin/api/policy/simulate"),
  executed("POST", "/auth/api/invite/accept"),
  executed("POST", "/auth/api/login", { required: ["/next"] }),
  executed("POST", "/auth/api/password-reset"),
  // Bodies from scripts/ui/identity_contract.py (the real handlers over fakes).
  executed("POST", "/gateway/v1/faces/likeness", { required: ["/object", "/overall"] }),
  executed("POST", "/gateway/v1/images/identity", { required: ["/data", "/likeness"] }),
  executed("POST", "/openai/v1/audio/transcriptions"),
  executed("POST", "/openai/v1/chat/completions"),
  executed("POST", "/v1/images/generations"),
  executed("POST", "/v1/videos"),
  executed("POST", "/admin/api/categories/candidates", { free: ["/snapshot"] }),
  executed("POST", "/auth/api/invite", { free: ["/plan/profile"] }),
  executed("POST", "/gateway/v1/media/jobs/image-generations"),
  executed("POST", "/me/api/apps/{id}/keys", { path: /^\/me\/api\/apps\/[^/]+\/keys$/ }),
  executed("POST", "/me/api/connection-test"),
];

export const lookup = (method: string, pathname: string) =>
  REGISTRY.find(
    (endpoint) =>
      endpoint.methods.includes(method) && endpoint.path.test(pathname),
  );

// What the gateway says when it refuses: real refusals from the same fixture
// server (a missing receipt, a missing asset, an unknown admin route), merged
// by intercept.ts so that only the keys every refusal has are required.
export async function errorSamples(api: APIRequestContext): Promise<unknown[]> {
  const bodies: unknown[] = [];
  for (const path of [
    "/me/api/receipts/none",
    "/me/api/media/none",
    "/admin/api/people/none",
  ]) {
    const response = await api.get(path);
    if (response.status() < 400)
      throw new Error(
        `intercept probe: GET ${path} was not refused (HTTP ${response.status()})`,
      );
    bodies.push(await response.json());
  }
  return bodies;
}
