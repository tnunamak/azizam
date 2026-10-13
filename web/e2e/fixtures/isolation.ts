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
// A test that changes owner-only or global state (the settings in
// GLOBAL_SETTINGS below, services, profiles, providers) carries the tag
// `@global-state`. It must run where no other test runs at the same time (a
// one-worker lane or the serial project). The fixture records the global
// settings before it and restores them after it through the admin API, then
// rereads them; a setting that does not come back fails the test. A test
// without the tag that changes a global setting fails too.
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

type Json = Record<string, unknown>;

type GlobalSetting = {
  name: string;
  read: (owner: OwnerApi) => Promise<{ value: unknown; revision?: string }>;
  write: (owner: OwnerApi, value: unknown, revision?: string) => Promise<void>;
};

type OwnerApi = {
  get: (path: string) => Promise<Json>;
  send: (
    method: "PUT" | "POST" | "DELETE",
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
    name: "sign-in",
    read: async (owner) => {
      const body = await owner.get("/admin/api/operator/sign-in");
      return { value: body.mode, revision: body.revision as string };
    },
    // The admin API refuses the seeded mode "both" while no identity provider
    // is enabled, so the fixture gateway's restore route writes it back.
    write: async (owner, value) => {
      await owner.send("PUT", "/fixture/operator-settings/sign_in", { mode: value });
    },
  },
  {
    name: "user-access-defaults",
    read: async (owner) => ({ value: (await overrides(owner)).user_access_defaults }),
    write: async (owner, value) => {
      await owner.send("PUT", "/admin/api/operator/user-access-defaults", value);
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

async function ownerApi(request: APIRequestContext): Promise<OwnerApi> {
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
      if (global) {
        // A global-state test needs the server to itself.
        expect(
          testInfo.config.workers === 1 || testInfo.project.name.startsWith("chromium-global-state"),
          `${GLOBAL_STATE_TAG} tests run in a one-worker lane or the global-state project, not ${testInfo.project.name} with ${testInfo.config.workers} workers`,
        ).toBe(true);
      }
      const owner = await ownerApi(request);
      const before = await snapshot(owner);
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
        const profiles = (await owner.get("/admin/api/profiles")).profiles as {
          id: string;
          name: string;
        }[];
        const profile = profiles.find((item) => item.name === plan);
        expect(profile, `access plan ${plan}`).toBeDefined();
        const email = isolated.email(`member${members.length + 1}`);
        const invite = (await owner.send("POST", "/admin/api/invites", {
          profile_id: profile!.id,
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
