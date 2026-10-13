import { request, type FullConfig } from "@playwright/test";
import { mkdir } from "node:fs/promises";

// Each fixture gateway in the config (one per distinct project baseURL) gets
// the same seed once, before any worker starts: one owner, one engine, one
// plan, one app with a key, and one real completion.
export default async function setup(config: FullConfig) {
  await mkdir("test-results/screens", { recursive: true });
  // The data namespace's run part (fixtures/isolation.ts); workers inherit it.
  process.env.UI_E2E_RUN_ID ??= Date.now().toString(36);
  const servers = new Map<string, string>();
  for (const project of config.projects) {
    const { baseURL, storageState } = project.use;
    if (typeof baseURL !== "string" || typeof storageState !== "string")
      throw new Error(`Project ${project.name} needs a baseURL and a storage-state file path`);
    if (servers.has(baseURL) && servers.get(baseURL) !== storageState)
      throw new Error(`Projects on ${baseURL} must share one storage-state file`);
    servers.set(baseURL, storageState);
  }
  await Promise.all([...servers].map(([baseURL, storagePath]) => seed(baseURL, storagePath)));
}

async function seed(baseURL: string, storagePath: string) {
  const api = await request.newContext({ baseURL });
  const owner = await api.post("/setup/local-owner", {
    form: {
      email: "owner@example.test",
      password: "Portable UI test password 2026",
      password_confirm: "Portable UI test password 2026",
    },
    maxRedirects: 0,
  });
  if (owner.status() !== 303)
    throw new Error(`Owner setup returned HTTP ${owner.status()}`);
  const session = await (await api.get("/admin/api/session")).json();
  const headers = { "x-azizam-csrf": session.actor.csrf_token };
  const post = async (url: string, data: unknown, extra = {}) => {
    const response = await api.post(url, {
      data,
      headers: { ...headers, ...extra },
    });
    if (!response.ok())
      throw new Error(`Seed ${url} returned HTTP ${response.status()}`);
    return response.json();
  };
  const port = Number(new URL(baseURL).port);
  const registry = await api.get("/admin/api/operator/service-registry");
  await post(
    "/admin/api/operator/services",
    {
      id: "capture-engine",
      name: "Local test engine",
      base_url: `http://127.0.0.1:${port + 1}`,
      protocol: "openai-compatible",
      capabilities: ["chat", "completions"],
      health_endpoint: "/health",
      models: [{ public: "demo-chat", upstream: "demo-chat" }],
      model_policy_schema_version: 1,
      enabled: true,
      note: "Isolated deterministic browser-test engine.",
    },
    { "if-match": registry.headers().etag },
  );
  await post("/admin/api/config/reload", {});
  const { access_plan: plan } = await post("/admin/api/access-plans", {
    name: "Local chat",
    can_use: { chat: { items: [{ model: "demo-chat" }] } },
    direct_engine_access: { engines: [], operations: [] },
    paid_providers: [],
    provider_fallback: false,
    caps: { usd_per_request: null, usd_per_day: null, usd_per_month: null },
    can_load_models: true,
    receipt_detail: "full",
    media_metadata: "none",
  });
  const { users } = await (await api.get("/admin/api/users")).json();
  const { app } = await post("/admin/api/apps", {
    name: "Browser test app",
    kind: "personal",
    owner_user_id: users[0].id,
    access_plan: plan.id,
  });
  const { credential } = await post(`/admin/api/apps/${app.id}/keys`, {
    label: "Seeded desktop key",
  });
  const completion = await api.post("/openai/v1/chat/completions", {
    headers: { authorization: `Bearer ${credential.api_key}` },
    data: {
      model: "demo-chat",
      messages: [
        { role: "user", content: "Check the isolated gateway connection." },
      ],
    },
  });
  if (!completion.ok()) {
    const failure = await completion.json();
    throw new Error(
      `Real seeded request returned HTTP ${completion.status()}: ${failure.error?.code ?? ""} ${failure.error?.message ?? ""}`,
    );
  }
  const result = await completion.json();
  if (!result.choices?.[0]?.message?.content)
    throw new Error("Seeded engine did not return a completion");
  await api.storageState({ path: storagePath });
  await api.dispose();
}
