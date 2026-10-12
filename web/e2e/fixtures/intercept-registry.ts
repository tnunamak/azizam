// Endpoints a browser test may stub with fulfilJson (intercept.ts). One entry
// per route family: the methods and path it covers, and `probe`, a read-only
// call on the fixture gateway the stub's server runs, made with the lane
// owner's session. The probe's real response gives the structure a stub must
// have. An endpoint that no safe call can reach (it changes state, or the seed
// has no data for it) says why in `unprobed`; its stubs are then checked only
// for the one thing every stub shares, that they are JSON.
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
  unprobed?: string;
  open?: string[];
  required?: string[];
}

async function read(api: APIRequestContext, path: string): Promise<unknown> {
  const response = await api.get(path);
  if (response.status() !== 200)
    throw new Error(`intercept probe GET ${path} returned HTTP ${response.status()}: ${(await response.text()).slice(0, 200)}`);
  return response.json();
}

const get = (path: string) => (api: APIRequestContext) => read(api, path);
const exact = (path: string) => new RegExp(`^${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);

type Spec = Omit<Endpoint, "id" | "path" | "methods"> & { methods?: string[]; path?: RegExp };

// Admin and member reads that the lane owner's session can make as they are.
const reads = (path: string, extra: Spec = {}): Endpoint => ({
  id: `GET ${path}`,
  methods: ["GET"],
  path: exact(path),
  probe: get(path),
  ...extra,
});

const unprobed = (method: string, path: string, reason: string, pattern?: RegExp): Endpoint => ({
  id: `${method} ${path}`,
  methods: [method],
  path: pattern ?? exact(path),
  unprobed: reason,
});

const SEED_HAS_NONE = "the fixture seed has no such data, and a probe may not create it";
const CHANGES_STATE = "it changes state, so a probe may not call it";

export const REGISTRY: Endpoint[] = [
  reads("/.well-known/azizam"),
  reads("/auth/api/options"),
  reads("/setup/api/state"),
  reads("/health"),
  reads("/showcase/static/sections.json"),
  reads("/showcase/examples/index.json"),
  reads("/admin/api/activity/requests"),
  reads("/admin/api/audit"),
  reads("/admin/api/categories/active"),
  reads("/admin/api/categories/history"),
  reads("/admin/api/categories/services"),
  reads("/admin/api/extensions"),
  reads("/admin/api/invites"),
  reads("/admin/api/oidc-config"),
  reads("/admin/api/operator/overrides"),
  reads("/admin/api/operator/service-registry"),
  reads("/admin/api/operator/sign-in"),
  reads("/admin/api/people"),
  reads("/admin/api/profiles"),
  reads("/admin/api/policy/editor"),
  reads("/admin/api/providers"),
  reads("/admin/api/providers/-/catalog"),
  reads("/admin/api/providers/openrouter/reconciliation"),
  reads("/admin/api/settings/defaults"),
  reads("/admin/api/settings/effective"),
  reads("/admin/api/settings/instance"),
  reads("/admin/api/settings/prompt-memory"),
  reads("/admin/api/settings/receipts"),
  reads("/admin/api/status"),
  reads("/admin/api/users"),
  reads("/me/api/home"),
  reads("/me/api/jobs"),
  reads("/me/api/media"),
  reads("/me/api/models/catalog"),
  reads("/me/api/session"),
  reads("/me/api/status"),
  reads("/me/api/usage/summary"),
  reads("/me/api/workflows"),
  {
    id: "GET /me/api/media/{id}",
    methods: ["GET"],
    path: /^\/me\/api\/media\/[^/]+$/,
    unprobed: SEED_HAS_NONE,
  },
  unprobed("DELETE", "/me/api/media/{id}", CHANGES_STATE, /^\/me\/api\/media\/[^/]+$/),
  unprobed("GET", "/me/api/receipts/{id}", SEED_HAS_NONE, /^\/me\/api\/receipts\/[^/]+$/),
  unprobed("GET", "/admin/api/invites/{id}/link", SEED_HAS_NONE, /^\/admin\/api\/invites\/[^/]+\/link$/),
  unprobed("GET", "/gateway/v1/media/jobs/{id}", SEED_HAS_NONE, /^\/gateway\/v1\/media\/jobs\/[^/]+$/),
  unprobed("GET", "/gateway/v1/media/jobs/{id}/content", SEED_HAS_NONE, /^\/gateway\/v1\/media\/jobs\/[^/]+\/content$/),
  unprobed("GET", "/v1/videos/{id}", SEED_HAS_NONE, /^\/v1\/videos\/[^/]+$/),
  unprobed("DELETE", "/admin/api/invites/{id}", CHANGES_STATE, /^\/admin\/api\/invites\/[^/]+$/),
  unprobed("DELETE", "/admin/api/people/{id}", CHANGES_STATE, /^\/admin\/api\/people\/[^/]+$/),
  unprobed("PATCH", "/admin/api/operator/services/{id}", CHANGES_STATE, /^\/admin\/api\/operator\/services\/[^/]+$/),
  unprobed("PATCH", "/admin/api/people/{id}", CHANGES_STATE, /^\/admin\/api\/people\/[^/]+$/),
  unprobed("PATCH", "/admin/api/profiles/{id}", CHANGES_STATE, /^\/admin\/api\/profiles\/[^/]+$/),
  unprobed("POST", "/admin/api/invites", CHANGES_STATE),
  unprobed("POST", "/admin/api/operator/services/test", CHANGES_STATE),
  unprobed("POST", "/admin/api/people/{id}/reset", CHANGES_STATE, /^\/admin\/api\/people\/[^/]+\/reset$/),
  unprobed("POST", "/admin/api/policy/simulate", CHANGES_STATE),
  unprobed("POST", "/auth/api/invite/accept", CHANGES_STATE),
  unprobed("POST", "/auth/api/login", CHANGES_STATE),
  unprobed("POST", "/auth/api/password-reset", CHANGES_STATE),
  unprobed("POST", "/gateway/v1/workflows/{id}/run", CHANGES_STATE, /^\/gateway\/v1\/workflows\/[^/]+\/run$/),
  unprobed("POST", "/openai/v1/audio/transcriptions", CHANGES_STATE),
  unprobed("POST", "/openai/v1/chat/completions", CHANGES_STATE),
  unprobed("POST", "/v1/images/generations", CHANGES_STATE),
  unprobed("POST", "/v1/videos", CHANGES_STATE),
  unprobed("POST", "/admin/api/categories/candidates", CHANGES_STATE),
  unprobed("POST", "/auth/api/invite", CHANGES_STATE),
  unprobed("POST", "/gateway/v1/media/jobs/image-generations", CHANGES_STATE),
  unprobed("POST", "/me/api/apps/{id}/keys", CHANGES_STATE, /^\/me\/api\/apps\/[^/]+\/keys$/),
  unprobed("POST", "/me/api/connection-test", CHANGES_STATE),
];

export const lookup = (method: string, pathname: string) =>
  REGISTRY.find((endpoint) => endpoint.methods.includes(method) && endpoint.path.test(pathname));

// What the gateway says when it refuses: real refusals from the same fixture
// server (a missing receipt, a missing asset, an unknown admin route), merged
// by intercept.ts so that only the keys every refusal has are required.
export async function errorSamples(api: APIRequestContext): Promise<unknown[]> {
  const bodies: unknown[] = [];
  for (const path of ["/me/api/receipts/none", "/me/api/media/none", "/admin/api/people/none"]) {
    const response = await api.get(path);
    if (response.status() < 400) throw new Error(`intercept probe: GET ${path} was not refused (HTTP ${response.status()})`);
    bodies.push(await response.json());
  }
  return bodies;
}
