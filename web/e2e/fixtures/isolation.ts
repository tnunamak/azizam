// Per-test data isolation for browser tests (docs/design/TEST_STRATEGY.md 3.4.7).
//
// Import `test` and `expect` from here instead of "@playwright/test". Every
// test then gets two teardown checks, and can ask for an `isolated` namespace:
//
// - `isolated.member()` creates a member principal with its own app, key and
//   signed-in browser context through the admin API. Principal-scoped state
//   (sessions, keys, quotas, rate limits, preferences) then belongs to that
//   member and never to the lane owner.
// - `isolated.name(text)` and `isolated.email(local)` give display names and
//   addresses no other invocation uses, also for two copies of one case.
// - Teardown fails the test if its member still has a queued or running media
//   job, or if the fake engine still holds a request tagged with its namespace.
//
// A test that changes owner-only or global state carries `@global-state` and
// runs on a one-worker lane. The fixture records the settings in
// GLOBAL_SETTINGS before each test and restores them after it. A service test
// can also opt into `@service-registry-state`; that check detects leftover
// service changes but does not restore them. Access-plan tests can opt into
// `@access-plan-definition`; it checks plan definitions, not their holders.
// Provider cap tests can opt into `@provider-cap-state`; it checks normalized
// global caps and non-empty caps for listed providers, not broader provider
// state. Other resource classes remain spec-owned until they have checks.
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import {
  test as base,
  expect,
  request as playwrightRequest,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type TestInfo,
} from "@playwright/test";

export { expect };

export const GLOBAL_STATE_TAG = "@global-state";
export const SERVICE_REGISTRY_STATE_TAG = "@service-registry-state";
export const ACCESS_PLAN_DEFINITION_TAG = "@access-plan-definition";
export const PROVIDER_CAP_STATE_TAG = "@provider-cap-state";

type Json = Record<string, unknown>;

type GlobalSetting = {
  name: string;
  read: (owner: OwnerApi) => Promise<{ value: unknown; revision?: string }>;
  write: (owner: OwnerApi, value: unknown, revision?: string) => Promise<void>;
};

type OwnerApi = {
  get: (path: string) => Promise<Json>;
  send: (
    method: "PATCH" | "PUT" | "POST" | "DELETE",
    path: string,
    data?: unknown,
    revision?: string,
  ) => Promise<Json>;
};

// Owner-global settings with a read and an idempotent write in the admin API.
// Revisions change on every write, so only values are compared.
const settingsPath = (name: string, field: string): GlobalSetting => ({
  name,
  read: async (owner) => {
    const body = await owner.get(`/admin/api/settings/${name}`);
    return { value: body[field], revision: body.revision as string };
  },
  write: async (owner, value, revision) => {
    await owner.send("PUT", `/admin/api/settings/${name}`, value, revision);
  },
});

const overrides = async (owner: OwnerApi) =>
  (await owner.get("/admin/api/operator/overrides")).overrides as Json;

async function serviceRegistryEntries(owner: OwnerApi): Promise<Json> {
  const registry = (await owner.get("/admin/api/operator/service-registry"))
    .service_registry as Json;
  return (registry.services ?? {}) as Json;
}

async function accessPlanDefinitions(owner: OwnerApi): Promise<Json> {
  const plans = (await owner.get("/admin/api/access-plans"))
    .access_plans as Array<{ id: string; plan: Json }>;
  return Object.fromEntries(plans.map(({ id, plan }) => [id, plan]));
}

function isJsonRecord(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function normalizedProviderCaps(value: unknown, label: string): Json {
  if (!isJsonRecord(value)) throw new Error(`${label} must be an object`);
  const allowed = ["daily_usd", "monthly_usd"];
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length)
    throw new Error(`${label} contains unsupported fields: ${unknown.sort().join(", ")}`);
  const caps: Json = {};
  for (const key of allowed) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
    const amount = value[key];
    if (typeof amount !== "number" || !Number.isFinite(amount) || amount < 0)
      throw new Error(`${label}.${key} must be a finite non-negative number`);
    caps[key] = amount;
  }
  return Object.fromEntries(Object.entries(caps).sort(([left], [right]) => left.localeCompare(right)));
}

/** Project only stable cap policy from the owner-facing provider API. */
export function providerCapPolicyProjection(payload: unknown): Json {
  if (!isJsonRecord(payload)) throw new Error("provider response must be an object");
  if (!isJsonRecord(payload.global)) throw new Error("provider response is missing global state");
  if (!Object.prototype.hasOwnProperty.call(payload.global, "caps"))
    throw new Error("provider response is missing global caps");
  if (!Array.isArray(payload.providers)) throw new Error("provider response is missing providers");

  const globalCaps = normalizedProviderCaps(payload.global.caps, "global caps");
  const providers: Record<string, Json> = {};
  const seen = new Set<string>();
  for (const [index, rawProvider] of payload.providers.entries()) {
    if (!isJsonRecord(rawProvider)) throw new Error(`provider ${index} must be an object`);
    if (typeof rawProvider.id !== "string" || !rawProvider.id.trim())
      throw new Error(`provider ${index} is missing its id`);
    const id = rawProvider.id.trim();
    if (seen.has(id)) throw new Error(`provider response contains duplicate id ${id}`);
    seen.add(id);
    if (!Object.prototype.hasOwnProperty.call(rawProvider, "caps"))
      throw new Error(`provider ${id} is missing caps`);
    const caps = normalizedProviderCaps(rawProvider.caps, `provider ${id} caps`);
    if (Object.keys(caps).length) providers[id] = caps;
  }
  return {
    global: globalCaps,
    providers: Object.fromEntries(Object.entries(providers).sort(([left], [right]) => left.localeCompare(right))),
  };
}

async function providerCapPolicy(owner: OwnerApi): Promise<Json> {
  return providerCapPolicyProjection(await owner.get("/admin/api/providers"));
}

function changedProviderCapPolicies(before: Json, after: Json): string[] {
  const changes: string[] = [];
  if (!same(before.global, after.global)) changes.push("global caps");
  const previous = before.providers as Json;
  const current = after.providers as Json;
  const ids = new Set([...Object.keys(previous), ...Object.keys(current)]);
  for (const id of [...ids].sort())
    if (!same(previous[id], current[id])) changes.push(`provider ${id}`);
  return changes;
}

export const GLOBAL_SETTINGS: GlobalSetting[] = [
  settingsPath("instance", "instance"),
  settingsPath("defaults", "defaults"),
  settingsPath("prompt-memory", "prompt_memory"),
  {
    name: "receipts",
    read: async (owner) => {
      const body = await owner.get("/admin/api/settings/receipts");
      return { value: body.retention_days, revision: body.revision as string };
    },
    write: async (owner, value, revision) => {
      await owner.send(
        "PUT",
        "/admin/api/settings/receipts",
        { retention_days: value, preview: null },
        revision,
      );
    },
  },
  {
    name: "sessions",
    read: async (owner) => {
      const body = await owner.get("/admin/api/settings/sessions");
      return { value: body.sessions, revision: body.revision as string };
    },
    write: async (owner, value, revision) => {
      await owner.send("PUT", "/admin/api/settings/sessions", value, revision);
    },
  },
  {
    name: "sign-in",
    read: async (owner) => {
      const body = await owner.get("/admin/api/signin");
      const p = body.provider as Json;
      return {
        value: {
          methods: body.methods,
          rules: body.rules,
          provider: {
            name: p.name,
            address: p.address,
            client_id: p.client_id,
            scopes: p.scopes,
            groups_claim: p.groups_claim,
            authorization_endpoint: p.authorization_endpoint,
            token_endpoint: p.token_endpoint,
            jwks_uri: p.jwks_uri,
            clock_skew_seconds: p.clock_skew_seconds,
          },
        },
        revision: body.revision as string,
      };
    },
    // A provider that was configured and off is written back off ("save for
    // later", which needs no test). One that was never configured has its
    // switches and rules put back through the owner's route, then its connection
    // blanked through the fixture gateway's own route (Save cannot blank it).
    // The saved client secret is never read or sent.
    write: async (owner, value, revision) => {
      const saved = value as { methods: { provider: boolean }; rules: unknown; provider: Json };
      const connected = Boolean(saved.provider.address && saved.provider.client_id);
      await owner.send(
        "PUT",
        "/admin/api/signin",
        {
          proposal: {
            methods: saved.methods,
            provider: { ...saved.provider, client_secret: "" },
            rules: saved.rules,
          },
          test_id: null,
          keep_admin_keys: [],
          save_for_later: connected && !saved.methods.provider,
        },
        revision,
      );
      if (connected) return;
      await owner.send(
        "PUT",
        "/fixture/operator-settings/oidc_provider_config",
        {
          enabled: false,
          issuer: "",
          client_id: "",
          redirect_uri: "",
          display_name: "",
          authorization_endpoint: "",
          token_endpoint: "",
          jwks_uri: "",
        },
      );
    },
  },
  {
    // "On shared computers" (Settings > Sign-in). The admin API saves it with the sign-in proposal, so the write sends
    // the saved connection and rules back as the server has them now and changes only the choice.
    name: "sign-in-memory",
    read: async (owner) => {
      const body = await owner.get("/admin/api/signin");
      return { value: body.memory, revision: body.revision as string };
    },
    write: async (owner, value) => {
      const saved = await owner.get("/admin/api/signin");
      const p = saved.provider as Json;
      await owner.send(
        "PUT",
        "/admin/api/signin",
        {
          proposal: {
            methods: saved.methods,
            provider: {
              name: p.name,
              address: p.address,
              client_id: p.client_id,
              client_secret: "",
              scopes: p.scopes,
              groups_claim: p.groups_claim,
              authorization_endpoint: p.authorization_endpoint,
              token_endpoint: p.token_endpoint,
              jwks_uri: p.jwks_uri,
              clock_skew_seconds: p.clock_skew_seconds,
            },
            rules: saved.rules,
            memory: value,
          },
          test_id: null,
          keep_admin_keys: [],
          save_for_later: false,
        },
        saved.revision as string,
      );
    },
  },
  {
    name: "media-job-admission",
    read: async (owner) => {
      const all = await overrides(owner);
      const revisions = all.revisions as Json;
      return {
        value: all.media_job_admission,
        revision: revisions.media_job_admission_overrides as string,
      };
    },
    write: async (owner, value, revision) => {
      await owner.send("PUT", "/admin/api/operator/media-job-admission", value, revision);
    },
  },
];

export async function ownerApi(request: APIRequestContext): Promise<OwnerApi> {
  const session = await request.get("/admin/api/session");
  expect(session.status(), "the lane owner session").toBe(200);
  const csrf = ((await session.json()) as { actor: { csrf_token: string } }).actor
    .csrf_token;
  const checked = async (label: string, response: Awaited<ReturnType<APIRequestContext["get"]>>) => {
    const text = await response.text();
    expect(response.ok(), `${label} returned HTTP ${response.status()}: ${text}`).toBe(true);
    return (text ? JSON.parse(text) : {}) as Json;
  };
  return {
    get: async (path) => checked(`GET ${path}`, await request.get(path)),
    send: async (method, path, data, revision) =>
      checked(
        `${method} ${path}`,
        await request.fetch(path, {
          method,
          data,
          headers: {
            "x-azizam-admin-action": "1",
            "x-azizam-csrf": csrf,
            ...(revision === undefined ? {} : { "if-match": revision }),
          },
        }),
      ),
  };
}

// The serial project runs one test at a time on its server, so whatever the
// fake engine or the media queue still holds afterwards belongs to that test,
// whether or not it used `isolated`.
export function fixtureStateDir(port: number): string {
  const root = process.env.UI_E2E_STATE_ROOT ?? path.resolve(__dirname, "../../.ui-e2e-state");
  const runs = (directory: string) =>
    readdirSync(directory, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(directory, entry.name));
  const active = [root, ...runs(root)]
    .flatMap(runs)
    .filter((directory) => path.basename(directory).startsWith("run-"))
    .filter(
      (directory) =>
        existsSync(path.join(directory, `${port}.log`)) &&
        readFileSync(path.join(directory, "config.yml"), "utf8").includes(`port: ${port}\n`),
    );
  if (active.length !== 1) throw new Error("Expected one disposable fixture state directory");
  return active[0];
}

async function noWorkLeft(request: APIRequestContext) {
  const port = Number(new URL((await request.get("/admin/api/session")).url()).port);
  const open = await request.get(`http://127.0.0.1:${port + 1}/fixture/requests?namespace=*`);
  expect(open.ok(), "read the fake engine's request log").toBe(true);
  expect((await open.json()).in_flight, "fake engine requests still open").toBe(0);
  const unfinished = execFileSync(
    "sqlite3",
    [
      "-readonly",
      "-cmd",
      ".timeout 10000",
      path.join(fixtureStateDir(port), "media_jobs.db"),
      "SELECT count(*) FROM media_jobs WHERE status IN ('queued','running')",
    ],
    { encoding: "utf8" },
  ).trim();
  expect(unfinished, "queued or running media jobs left behind").toBe("0");
}

type Snapshot = Map<string, { value: unknown; revision?: string }>;

async function snapshot(owner: OwnerApi): Promise<Snapshot> {
  const entries = await Promise.all(
    GLOBAL_SETTINGS.map(async (setting) => [setting.name, await setting.read(owner)] as const),
  );
  return new Map(entries);
}

const same = (left: unknown, right: unknown) =>
  JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));

// Key order does not matter, and a field set to null, [] or {} equals an
// absent one: the API stores a written override document with every field
// filled in, while an unset setting reads as {}.
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.keys(value as Json)
        .sort()
        .map((key) => [key, canonical((value as Json)[key])])
        .filter(([, item]) => !empty(item)),
    );
  return value;
}

const empty = (value: unknown) =>
  value === null ||
  value === undefined ||
  (Array.isArray(value) && value.length === 0) ||
  (typeof value === "object" && Object.keys(value as Json).length === 0);

function changed(before: Snapshot, after: Snapshot): string[] {
  return GLOBAL_SETTINGS.map((setting) => setting.name).filter(
    (name) => !same(before.get(name)?.value, after.get(name)?.value),
  );
}

function changedRecordIds(before: Json, after: Json): string[] {
  const ids = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...ids].filter((id) => !same(before[id], after[id])).sort();
}

export type Member = {
  email: string;
  password: string;
  userId: string;
  appId: string;
  key: string;
  context: BrowserContext;
  page: Page;
};

export type Isolated = {
  /** `<run>-<lane>-<project>-<worker>-<invocation>`, unique per invocation. */
  namespace: string;
  /** A display name no other invocation uses. */
  name: (text: string) => string;
  /** An address no other invocation uses. */
  email: (local: string) => string;
  /** Text the fake engine files under this namespace (see fake_engine.py). */
  tag: (text: string) => string;
  /** A new member principal with its own app, key and signed-in context. */
  member: (options?: { plan?: string; name?: string }) => Promise<Member>;
};

let invocations = 0;

function namespaceFor(testInfo: TestInfo): string {
  const run = process.env.UI_E2E_RUN_ID ?? "local";
  const lane = process.env.UI_E2E_LANE ?? process.env.UI_E2E_PORT ?? "lane";
  invocations += 1;
  return [run, lane, testInfo.project.name, `w${testInfo.workerIndex}`, `i${invocations}`]
    .join("-")
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-");
}

// The parallel pool may run on several gateways (playwright.config.ts). A
// worker keeps one server for its life: parallelIndex picks it, so a test sees
// the server whose data it created. Only the `chromium` project is spread; the
// `chromium-pool-N` projects exist to be seeded and run nothing.
function poolProject(testInfo: TestInfo) {
  if (testInfo.project.name !== "chromium") return undefined;
  const pool = testInfo.config.projects.filter(
    (project) => project.name === "chromium" || project.name.startsWith("chromium-pool-"),
  );
  return pool.length > 1 ? pool[testInfo.parallelIndex % pool.length] : undefined;
}

export const test = base.extend<{ isolated: Isolated; globalStateGuard: void }>({
  baseURL: async ({ baseURL }, use, testInfo) => {
    await use(poolProject(testInfo)?.use.baseURL ?? baseURL);
  },
  storageState: async ({ storageState }, use, testInfo) => {
    await use(poolProject(testInfo)?.use.storageState ?? storageState);
  },
  globalStateGuard: [
    async ({ request }, use, testInfo) => {
      const global = testInfo.tags.includes(GLOBAL_STATE_TAG);
      const providerCaps = testInfo.tags.includes(PROVIDER_CAP_STATE_TAG);
      if (global) {
        // A global-state test needs the server to itself.
        expect(
          testInfo.config.workers === 1 || testInfo.project.name.startsWith("chromium-global-state"),
          `${GLOBAL_STATE_TAG} tests run in a one-worker lane or the global-state project, not ${testInfo.project.name} with ${testInfo.config.workers} workers`,
        ).toBe(true);
      }
      if (providerCaps)
        expect(
          global,
          `${PROVIDER_CAP_STATE_TAG} tests also require ${GLOBAL_STATE_TAG} serialization`,
        ).toBe(true);
      const owner = await ownerApi(request);
      const before = await snapshot(owner);
      const registryBefore = testInfo.tags.includes(SERVICE_REGISTRY_STATE_TAG)
        ? await serviceRegistryEntries(owner)
        : undefined;
      const plansBefore = testInfo.tags.includes(ACCESS_PLAN_DEFINITION_TAG)
        ? await accessPlanDefinitions(owner)
        : undefined;
      const providerCapsBefore = providerCaps ? await providerCapPolicy(owner) : undefined;
      await use();
      // Every step below runs even if an earlier one fails, so one broken
      // restore never leaves the rest of the server dirty for the next test.
      const problems: string[] = [];
      const attempt = async (label: string, step: () => Promise<void>) => {
        try {
          await step();
        } catch (error) {
          problems.push(`${label}: ${error instanceof Error ? error.message : String(error)}`);
        }
      };
      let touched: string[] = [];
      await attempt("read settings after the test", async () => {
        const owner2 = await ownerApi(request);
        const after = await snapshot(owner2);
        touched = changed(before, after);
        // Restore even for an undeclared change, so one violation does not
        // spread to the tests after it; the test still fails below.
        for (const name of touched) {
          const setting = GLOBAL_SETTINGS.find((item) => item.name === name)!;
          await attempt(`restore ${name}`, () =>
            setting.write(owner2, before.get(name)!.value, after.get(name)!.revision),
          );
        }
        const rechecked = changed(before, await snapshot(owner2));
        if (rechecked.length)
          problems.push(`global settings the fixture could not restore: ${rechecked.join(", ")}`);
      });
      if (registryBefore) {
        await attempt("read service registry after the test", async () => {
          const owner2 = await ownerApi(request);
          const registryAfter = await serviceRegistryEntries(owner2);
          const touchedServices = changedRecordIds(registryBefore, registryAfter);
          if (touchedServices.length)
            problems.push(
              `service registry changes left by the test: ${touchedServices.join(", ")}`,
            );
        });
      }
      if (plansBefore) {
        await attempt("read access plan definitions after the test", async () => {
          const owner2 = await ownerApi(request);
          const plansAfter = await accessPlanDefinitions(owner2);
          const touchedPlans = changedRecordIds(plansBefore, plansAfter);
          if (touchedPlans.length)
            problems.push(
              `access plan definitions left by the test: ${touchedPlans.join(", ")}`,
            );
        });
      }
      if (providerCapsBefore) {
        await attempt("read provider cap policy after the test", async () => {
          const providerCapsAfter = await providerCapPolicy(await ownerApi(request));
          const touchedCaps = changedProviderCapPolicies(providerCapsBefore, providerCapsAfter);
          if (touchedCaps.length)
            problems.push(`provider cap policy changes left by the test: ${touchedCaps.join(", ")}`);
        });
      }
      if (global) await attempt("work left running", () => noWorkLeft(request));
      if (!global && touched.length)
        problems.push(
          `global settings this test changed: ${touched.join(", ")}; tag it ${GLOBAL_STATE_TAG} or leave them alone`,
        );
      if (problems.length) throw new Error(`teardown found a dirty server:\n${problems.join("\n")}`);
    },
    { auto: true },
  ],
  isolated: async ({ browser, request, baseURL }, use, testInfo) => {
    const namespace = namespaceFor(testInfo);
    testInfo.annotations.push({ type: "namespace", description: namespace });
    const members: Member[] = [];
    const isolated: Isolated = {
      namespace,
      name: (text) => `${text} ${namespace}`,
      email: (local) => `${local}+${namespace}@example.test`,
      tag: (text) => `${text} [ns:${namespace}]`,
      member: async ({ plan = "Local chat", name = "Isolated Member" } = {}) => {
        const owner = await ownerApi(request);
        const plans = (await owner.get("/admin/api/access-plans")).access_plans as {
          id: string;
          name: string;
        }[];
        const found = plans.find((item) => item.name === plan);
        expect(found, `access plan ${plan}`).toBeDefined();
        const email = isolated.email(`member${members.length + 1}`);
        const invite = (await owner.send("POST", "/admin/api/invites", {
          access_plan: found!.id,
          email,
        })) as { url: string };
        const token = new URL(invite.url, baseURL).hash.replace(/^#token=/, "");
        const password = `Isolated member password ${namespace}`;
        const guest = await playwrightRequest.newContext({
          baseURL,
          storageState: { cookies: [], origins: [] },
        });
        try {
          const accepted = await guest.post("/auth/api/invite/accept", {
            data: { token, email, name: isolated.name(name), password },
          });
          const body = await accepted.json();
          expect(accepted.ok(), `accept the invite: ${JSON.stringify(body)}`).toBe(true);
          const context = await browser.newContext({
            baseURL,
            storageState: await guest.storageState(),
          });
          const member: Member = {
            email,
            password,
            userId: body.app.owner_user_id,
            appId: body.app.id,
            key: body.credential.api_key,
            context,
            page: await context.newPage(),
          };
          members.push(member);
          return member;
        } finally {
          await guest.dispose();
        }
      },
    };
    await use(isolated);
    try {
      for (const member of members) {
        for (const status of ["queued", "running"]) {
          const jobs = await request.get(`/gateway/v1/media/jobs?status=${status}`, {
            headers: { authorization: `Bearer ${member.key}` },
          });
          expect(jobs.ok(), `list ${member.email}'s ${status} jobs`).toBe(true);
          const ids = ((await jobs.json()).jobs as { id: string }[]).map((job) => job.id);
          expect(ids, `${member.email} left ${status} media jobs`).toEqual([]);
        }
      }
      const enginePort = Number(new URL(baseURL!).port) + 1;
      const pending = await request.get(
        `http://127.0.0.1:${enginePort}/fixture/requests?namespace=${encodeURIComponent(namespace)}`,
      );
      expect(pending.ok(), "read the fake engine's request log").toBe(true);
      expect(
        (await pending.json()).in_flight,
        "fake engine requests still open for this namespace",
      ).toBe(0);
    } finally {
      for (const member of members) await member.context.close();
    }
  },
});
