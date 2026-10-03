import { request, type FullConfig } from "@playwright/test";
import { mkdir } from "node:fs/promises";

export default async function seed(config: FullConfig) {
  await mkdir("test-results/screens", { recursive: true });
  const baseURL = config.projects[0].use.baseURL!;
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
  const headers = { "x-ai-gateway-csrf": session.actor.csrf_token };
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
  const { profile } = await post("/admin/api/profiles", {
    name: "Local chat",
    description: "Chat through the isolated test engine",
    profile: {
      id: "local-chat",
      protocols: ["openai"],
      backends: ["capture-engine"],
      models: ["demo-chat"],
      status_visibility: { level: "full" },
    },
  });
  const { users } = await (await api.get("/admin/api/users")).json();
  const { app } = await post("/admin/api/apps", {
    name: "Browser test app",
    kind: "personal",
    owner_user_id: users[0].id,
    profile_id: profile.id,
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
  await api.storageState({ path: "test-results/auth.json" });
  await api.dispose();
}
