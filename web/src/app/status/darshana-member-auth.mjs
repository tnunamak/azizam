import path from "node:path";
import { mkdir } from "node:fs/promises";

// Fixture credentials only. This script refuses operator ports.
export default async function login(browser) {
  const baseURL = process.env.APP_URL ?? "http://127.0.0.1:18885";
  const url = new URL(baseURL);
  if (
    url.hostname !== "127.0.0.1" ||
    Number(url.port) < 18000 ||
    Number(url.port) > 18998
  )
    throw new Error("Status review requires an isolated fixture gateway");
  const context = await browser.newContext({ baseURL });
  const email = "status-review@example.test";
  const password = "Portable UI test password 2026";
  try {
    let response = await context.request.post("/auth/api/login", {
      data: { email, password },
    });
    if (!response.ok()) {
      const owner = await browser.newContext({ baseURL });
      try {
        const signedIn = await owner.request.post("/auth/api/login", {
          data: { email: "owner@example.test", password },
        });
        if (!signedIn.ok())
          throw new Error(`Owner sign-in returned ${signedIn.status()}`);
        const session = await (
          await owner.request.get("/admin/api/session")
        ).json();
        const invites = await (
          await owner.request.get("/admin/api/invites")
        ).json();
        const profile = invites.profiles.find(
          (item) => item.name === "Local chat",
        );
        if (!profile) throw new Error("Seeded Local chat access plan missing");
        const invited = await owner.request.post("/admin/api/invites", {
          headers: {
            "x-ai-gateway-csrf": session.actor.csrf_token,
            "x-ai-gateway-admin-action": "1",
          },
          data: { profile_id: profile.id, email, expires_in: 3600 },
        });
        if (!invited.ok())
          throw new Error(`Fixture invite returned ${invited.status()}`);
        const invitation = await invited.json();
        const token = new URL(invitation.url, baseURL).hash.slice(
          "#token=".length,
        );
        response = await context.request.post("/auth/api/invite/accept", {
          data: { token, email, name: "Status review member", password },
        });
      } finally {
        await owner.close();
      }
    }
    if (!response.ok())
      throw new Error(`Member sign-in returned ${response.status()}`);
    const file = path.resolve(
      `test-results/status-member-auth-${url.port}.json`,
    );
    await mkdir(path.dirname(file), { recursive: true });
    await context.storageState({ path: file });
    return file;
  } finally {
    await context.close();
  }
}
