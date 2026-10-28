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
  GLOBAL_STATE_TAG,
  SERVICE_REGISTRY_STATE_TAG,
  test,
} from "./fixtures/isolation";

let leakedServiceId: string | undefined;

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

test.afterAll(async ({ request }) => {
  if (leakedServiceId) await removeProofService(request, leakedServiceId);
});

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
