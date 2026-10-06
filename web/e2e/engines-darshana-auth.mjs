import path from "node:path";

// Only for scripts/ui/serve.py's isolated gateway and synthetic test owner.
export default async function login(browser) {
  const origin = process.env.ENGINES_REVIEW_ORIGIN;
  const url = new URL(origin ?? "");
  const port = Number(url.port);
  if (url.hostname !== "127.0.0.1" || port < 18000 || port > 18997) {
    throw new Error(
      "ENGINES_REVIEW_ORIGIN must name an isolated loopback gateway on 18000-18997",
    );
  }
  const context = await browser.newContext();
  const api = context.request;
  const owner = await api.post(`${origin}/setup/local-owner`, {
    form: {
      email: "owner@example.test",
      password: "Portable UI test password 2026",
      password_confirm: "Portable UI test password 2026",
    },
    maxRedirects: 0,
  });
  if (owner.status() !== 303) {
    const signedIn = await api.post(`${origin}/auth/api/login`, {
      data: {
        email: "owner@example.test",
        password: "Portable UI test password 2026",
      },
    });
    if (!signedIn.ok())
      throw new Error("Synthetic review owner could not sign in");
  }
  const session = await (await api.get(`${origin}/admin/api/session`)).json();
  let registry = await api.get(`${origin}/admin/api/operator/service-registry`);
  const headers = { "x-azizam-csrf": session.actor.csrf_token };
  for (const [id, name, enginePort] of [
    ["review-chat", "Local review engine", port + 1],
    ["review-offline", "Offline review engine", 18498],
  ]) {
    const added = await api.post(`${origin}/admin/api/operator/services`, {
      headers: { ...headers, "if-match": registry.headers().etag },
      data: {
        id,
        name,
        base_url: `http://127.0.0.1:${enginePort}/v1`,
        protocol: "openai-compatible",
        capabilities: ["chat"],
        health_endpoint: "/health",
        enabled: true,
        role: "optional",
      },
    });
    if (!added.ok())
      throw new Error(`Review engine seed returned HTTP ${added.status()}`);
    registry = await api.get(`${origin}/admin/api/operator/service-registry`);
  }
  const page = await context.newPage();
  await page.goto(`${origin}/ui/engines/`);
  await page.getByRole("heading", { name: "Engines", level: 1 }).waitFor();
  const state = path.resolve("test-results/engines-darshana-auth.json");
  await context.storageState({ path: state });
  await context.close();
  return state;
}
