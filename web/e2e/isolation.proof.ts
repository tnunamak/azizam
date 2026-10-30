// Proof cases for fixtures/isolation.ts (TEST_STRATEGY.md 7, BI2a). Not a gate
// spec: scripts/e2e/prove-isolation runs this file with repeats and parallel
// workers against one server. The `test.fail()` cases prove that teardown
// fails a test which leaves state behind; Playwright reports them as passed
// only when the fixture really failed them.
import {
  request as playwrightRequest,
  type APIRequestContext,
} from "@playwright/test";
import {
  expect,
  ACCESS_PLAN_DEFINITION_TAG,
  GLOBAL_STATE_TAG,
  PROVIDER_CAP_STATE_TAG,
  SERVICE_REGISTRY_STATE_TAG,
  ownerApi,
  providerCapPolicyProjection,
  test,
} from "./fixtures/isolation";

let leakedServiceId: string | undefined;
let leakedAccessPlanId: string | undefined;
let leakedProviderId: string | undefined;
let leakedGlobalCapsBaseline: Record<string, unknown> | undefined;

async function createProofService(
  request: APIRequestContext,
  baseURL: string,
  name: string,
): Promise<string> {
  const session = await (await request.get("/admin/api/session")).json();
  const registry = await request.get("/admin/api/operator/service-registry");
  const created = await request.post("/admin/api/operator/services", {
    headers: {
      "x-azizam-csrf": session.actor.csrf_token,
      "if-match": registry.headers().etag,
    },
    data: {
      definition: "openai-compatible",
      name,
      disabled: true,
      values: {
        address: `http://127.0.0.1:${Number(new URL(baseURL).port) + 1}/provider/v1`,
      },
    },
  });
  expect(created.status()).toBe(201);
  const serviceId = (await created.json()).service_id as string;
  expect(serviceId).toBeTruthy();
  return serviceId;
}

async function removeProofService(
  request: APIRequestContext,
  serviceId: string,
): Promise<void> {
  const session = await (await request.get("/admin/api/session")).json();
  const registry = await request.get("/admin/api/operator/service-registry");
  const removed = await request.delete(
    `/admin/api/operator/services/${serviceId}`,
    {
      headers: {
        "x-azizam-csrf": session.actor.csrf_token,
        "if-match": registry.headers().etag,
      },
    },
  );
  expect(removed.ok()).toBe(true);
}

async function createProofAccessPlan(
  request: APIRequestContext,
  name: string,
): Promise<string> {
  const session = await (await request.get("/admin/api/session")).json();
  const created = await request.post("/admin/api/access-plans", {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
    data: {
      name,
      can_use: { chat: { items: "all" } },
      direct_engine_access: { engines: [], operations: [] },
      paid_providers: [],
      provider_fallback: false,
      caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
      can_load_models: true,
      receipt_detail: "basic",
      media_metadata: "none",
    },
  });
  expect(created.status()).toBe(201);
  const planId = (await created.json()).access_plan.id as string;
  expect(planId).toBeTruthy();
  return planId;
}

async function removeProofAccessPlan(
  request: APIRequestContext,
  planId: string,
): Promise<void> {
  const session = await (await request.get("/admin/api/session")).json();
  const removed = await request.delete(`/admin/api/access-plans/${planId}`, {
    headers: { "x-azizam-csrf": session.actor.csrf_token },
  });
  expect(removed.ok()).toBe(true);
}

async function createProofProvider(
  request: APIRequestContext,
  namespace: string,
): Promise<string> {
  const providerId = `capproof-${namespace}`;
  const owner = await ownerApi(request);
  const created = await owner.send("POST", "/admin/api/providers", {
    preset_id: "openrouter",
    provider_id: providerId,
    name: "Provider cap isolation proof",
    key_mode: "env",
    api_key_env: "AZIZAM_E2E_UNUSED_PROVIDER_KEY",
    enabled: false,
    models: [],
    expected_generation: null,
  });
  expect((created.provider as { id?: string }).id).toBe(providerId);
  return providerId;
}

async function removeProofProvider(
  request: APIRequestContext,
  providerId: string,
): Promise<void> {
  const owner = await ownerApi(request);
  await owner.send("DELETE", `/admin/api/providers/${encodeURIComponent(providerId)}`, {
    expected_generation: null,
  });
  const providers = (await owner.get("/admin/api/providers")).providers as { id: string }[];
  expect(providers.some((provider) => provider.id === providerId)).toBe(false);
}

async function restoreGlobalCaps(
  owner: Awaited<ReturnType<typeof ownerApi>>,
  caps: Record<string, unknown>,
): Promise<void> {
  if (Object.keys(caps).length)
    await owner.send("PUT", "/admin/api/providers/-/global-caps", caps);
  else await owner.send("DELETE", "/admin/api/providers/-/global-caps");
}

function changedCaps(caps: Record<string, unknown>): Record<string, unknown> {
  return { ...caps, daily_usd: caps.daily_usd === 1 ? 2 : 1 };
}

test.afterAll(async ({ request }) => {
  if (leakedServiceId) await removeProofService(request, leakedServiceId);
  if (leakedAccessPlanId) await removeProofAccessPlan(request, leakedAccessPlanId);
  if (leakedGlobalCapsBaseline !== undefined) {
    const owner = await ownerApi(request);
    await restoreGlobalCaps(owner, leakedGlobalCapsBaseline);
    const restored = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
    expect(restored.global).toEqual(leakedGlobalCapsBaseline);
  }
  // This is a disposable, disabled provider created by this proof alone. It has
  // no key value, access-plan references, spend or requests.
  if (leakedProviderId) await removeProofProvider(request, leakedProviderId);
});

test("provider cap projection ignores spend, status, and empty-cap inventory", async () => {
  const baseline = providerCapPolicyProjection({
    global: { caps: { monthly_usd: 20, daily_usd: 4 } },
    providers: [
      {
        id: "z-empty",
        caps: {},
        spend: { daily_usd: 1 },
        checked_at: "old",
        key_status: { state: "missing" },
      },
      {
        id: "alpha",
        caps: { monthly_usd: 3 },
        spend: { monthly_usd: 1 },
        checked_at: "old",
        key_status: { state: "set" },
      },
      {
        id: "paid",
        caps: { monthly_usd: 12, daily_usd: 3 },
        spend: { daily_usd: 2 },
        checked_at: "old",
        key_status: { state: "set", suffix: "synthetic" },
      },
    ],
  });
  const volatileOnly = providerCapPolicyProjection({
    global: { caps: { daily_usd: 4, monthly_usd: 20 }, spend: { daily_usd: 9 } },
    providers: [
      {
        id: "added-empty",
        caps: {},
        spend: {},
        checked_at: "new",
        key_status: { state: "unknown" },
      },
      {
        id: "paid",
        caps: { daily_usd: 3, monthly_usd: 12 },
        spend: { daily_usd: 8 },
        checked_at: "new",
        key_status: { state: "missing" },
      },
      {
        id: "alpha",
        caps: { monthly_usd: 3 },
        spend: { monthly_usd: 7 },
        checked_at: "new",
        key_status: { state: "unknown" },
      },
    ],
  });
  expect(volatileOnly).toEqual(baseline);
  expect(
    providerCapPolicyProjection({
      global: { caps: { daily_usd: 4, monthly_usd: 20 } },
      providers: [{ id: "paid", caps: { daily_usd: 3, monthly_usd: 13 } }],
    }),
  ).not.toEqual(baseline);
  expect(
    providerCapPolicyProjection({
      global: { caps: { daily_usd: 4, monthly_usd: 20 } },
      providers: [{ id: "paid", caps: { daily_usd: 3, monthly_usd: 12 } }],
    }),
  ).not.toEqual(baseline);
  expect(() =>
    providerCapPolicyProjection({ global: {}, providers: [] }),
  ).toThrow("missing global caps");
  expect(() =>
    providerCapPolicyProjection({ global: { caps: {} }, providers: [{ id: "bad" }] }),
  ).toThrow("missing caps");
});

test(
  "teardown fails a test that leaves global provider caps",
  { tag: [GLOBAL_STATE_TAG, PROVIDER_CAP_STATE_TAG] },
  async ({ request }) => {
    test.fail();
    const owner = await ownerApi(request);
    const baseline = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
    leakedGlobalCapsBaseline = baseline.global as Record<string, unknown>;
    await owner.send(
      "PUT",
      "/admin/api/providers/-/global-caps",
      changedCaps(leakedGlobalCapsBaseline),
    );
  },
);

test(
  "restored global provider caps satisfy the opt-in guard",
  { tag: [GLOBAL_STATE_TAG, PROVIDER_CAP_STATE_TAG] },
  async ({ request }) => {
    const owner = await ownerApi(request);
    const baseline = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
    const before = baseline.global as Record<string, unknown>;
    try {
      await owner.send(
        "PUT",
        "/admin/api/providers/-/global-caps",
        changedCaps(before),
      );
      const changed = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
      expect(changed.global).not.toEqual(before);
    } finally {
      await restoreGlobalCaps(owner, before);
    }
    expect(providerCapPolicyProjection(await owner.get("/admin/api/providers"))).toEqual(baseline);
  },
);

test(
  "teardown fails a test that leaves per-provider caps",
  { tag: [GLOBAL_STATE_TAG, PROVIDER_CAP_STATE_TAG, SERVICE_REGISTRY_STATE_TAG] },
  async ({ isolated, request }) => {
    test.fail();
    leakedProviderId = await createProofProvider(request, isolated.namespace);
    const owner = await ownerApi(request);
    await owner.send(
      "PUT",
      `/admin/api/providers/${encodeURIComponent(leakedProviderId)}/caps`,
      { daily_usd: 3 },
    );
  },
);

test(
  "cleared per-provider caps satisfy the opt-in guard",
  { tag: [GLOBAL_STATE_TAG, PROVIDER_CAP_STATE_TAG, SERVICE_REGISTRY_STATE_TAG] },
  async ({ isolated, request }) => {
    const owner = await ownerApi(request);
    const baseline = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
    const providerId = await createProofProvider(request, isolated.namespace);
    try {
      await owner.send(
        "PUT",
        `/admin/api/providers/${encodeURIComponent(providerId)}/caps`,
        { daily_usd: 3 },
      );
      const changed = providerCapPolicyProjection(await owner.get("/admin/api/providers"));
      expect((changed.providers as Record<string, unknown>)[providerId]).toEqual({ daily_usd: 3 });
      await owner.send("DELETE", `/admin/api/providers/${encodeURIComponent(providerId)}/caps`);
      expect(providerCapPolicyProjection(await owner.get("/admin/api/providers"))).toEqual(baseline);
    } finally {
      await removeProofProvider(request, providerId);
    }
  },
);

test("a member's requests are filed under its own namespace", async ({ isolated, request }) => {
  const member = await isolated.member();
  for (const turn of [1, 2]) {
    const response = await request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${member.key}` },
      data: {
        model: "demo-chat",
        messages: [{ role: "user", content: isolated.tag(`turn ${turn}`) }],
      },
    });
    expect(response.status()).toBe(200);
  }
  const engine = Number(new URL(test.info().project.use.baseURL!).port) + 1;
  const log = await request.get(
    `http://127.0.0.1:${engine}/fixture/requests?namespace=${isolated.namespace}`,
  );
  // A second copy running at the same time must not add to this count.
  expect(await log.json()).toEqual({ in_flight: 0, requests: 2 });
  await member.page.goto("/");
  await expect(member.page.getByRole("heading", { level: 1 })).toBeVisible();
});

test("teardown fails a test that changes a global setting without the tag", async ({ request }) => {
  test.fail();
  const session = await (await request.get("/admin/api/session")).json();
  const current = await (await request.get("/admin/api/settings/instance")).json();
  const saved = await request.put("/admin/api/settings/instance", {
    headers: {
      "x-azizam-admin-action": "1",
      "x-azizam-csrf": session.actor.csrf_token,
      "if-match": current.revision,
    },
    data: { ...current.instance, name: "Left behind by a test" },
  });
  expect(saved.status()).toBe(200);
});

test(
  "teardown fails a test that leaves a service registry record",
  { tag: [GLOBAL_STATE_TAG, SERVICE_REGISTRY_STATE_TAG] },
  async ({ isolated, request, baseURL }) => {
    test.fail();
    leakedServiceId = await createProofService(
      request,
      baseURL!,
      isolated.name("Unclean service registry proof"),
    );
  },
);

test(
  "a cleaned service registry record satisfies the opt-in guard",
  { tag: [GLOBAL_STATE_TAG, SERVICE_REGISTRY_STATE_TAG] },
  async ({ isolated, request, baseURL }) => {
    const serviceId = await createProofService(
      request,
      baseURL!,
      isolated.name("Clean service registry proof"),
    );
    try {
      const registry = await request.get("/admin/api/operator/service-registry");
      const services = (await registry.json()).service_registry.services;
      expect(services[serviceId]).toBeTruthy();
    } finally {
      await removeProofService(request, serviceId);
    }
  },
);

test(
  "teardown fails a test that leaves an access plan definition",
  { tag: [GLOBAL_STATE_TAG, ACCESS_PLAN_DEFINITION_TAG] },
  async ({ isolated, request }) => {
    test.fail();
    leakedAccessPlanId = await createProofAccessPlan(
      request,
      isolated.name("Unclean access plan proof"),
    );
  },
);

test(
  "a deleted access plan definition satisfies the opt-in guard",
  { tag: [GLOBAL_STATE_TAG, ACCESS_PLAN_DEFINITION_TAG] },
  async ({ isolated, request }) => {
    const planId = await createProofAccessPlan(
      request,
      isolated.name("Clean access plan proof"),
    );
    try {
      const planList = await request.get("/admin/api/access-plans");
      expect(planList.status()).toBe(200);
      const plans = (await planList.json()).access_plans as { id: string }[];
      expect(plans.some((plan) => plan.id === planId)).toBe(true);
    } finally {
      await removeProofAccessPlan(request, planId);
    }
  },
);

test.describe("an open upstream request", () => {
  let namespace = "";
  // After the fixture's teardown has seen the open request.
  test.afterAll(async () => {
    const engine = Number(new URL(test.info().project.use.baseURL!).port) + 1;
    const api = await playwrightRequest.newContext();
    await api.post(`http://127.0.0.1:${engine}/fixture/release?namespace=${namespace}`);
    await api.dispose();
  });

  test("teardown fails a test that leaves an upstream request open", async ({ isolated, request }) => {
    test.fail();
    namespace = isolated.namespace;
    const member = await isolated.member();
    const engine = Number(new URL(test.info().project.use.baseURL!).port) + 1;
    void member.page.request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${member.key}` },
      data: { model: "demo-chat", messages: [{ role: "user", content: isolated.tag("[hold]") }] },
    }).catch(() => undefined);
    await expect
      .poll(async () => (await (await request.get(
        `http://127.0.0.1:${engine}/fixture/requests?namespace=${namespace}`,
      )).json()).in_flight)
      .toBe(1);
  });
});

test.describe("an open request nobody tagged", () => {
  const stray = "proof-stray-request";
  test.afterAll(async () => {
    const engine = Number(new URL(test.info().project.use.baseURL!).port) + 1;
    const api = await playwrightRequest.newContext();
    await api.post(`http://127.0.0.1:${engine}/fixture/release?namespace=${stray}`);
    await api.dispose();
  });

  // The serial project checks the engine's total, so a request that belongs to
  // no test's namespace still fails the test that left it open.
  test("teardown fails a global-state test that leaves an untagged request open", { tag: "@global-state" }, async ({ isolated, request }) => {
    test.fail();
    const member = await isolated.member();
    const engine = Number(new URL(test.info().project.use.baseURL!).port) + 1;
    void member.page.request.post("/openai/v1/chat/completions", {
      headers: { authorization: `Bearer ${member.key}` },
      data: { model: "demo-chat", messages: [{ role: "user", content: `[hold] [ns:${stray}]` }] },
    }).catch(() => undefined);
    await expect
      .poll(async () => (await (await request.get(
        `http://127.0.0.1:${engine}/fixture/requests?namespace=${stray}`,
      )).json()).in_flight)
      .toBe(1);
  });
});
