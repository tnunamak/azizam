import { expect, test } from "@playwright/test";
import { authenticatedContext } from "../../fixtures/gateway/authenticated";

const bootstrapPath = process.env.P1B_BOOTSTRAP_PATH;

test.skip(!bootstrapPath, "P1B bootstrap artifact was not supplied");

test("uses signed sessions, CSRF, and owner boundaries", async ({ browser }) => {
  const admin = await authenticatedContext(browser, bootstrapPath!, "admin");
  const userA = await authenticatedContext(browser, bootstrapPath!, "user_a");
  const userB = await authenticatedContext(browser, bootstrapPath!, "user_b");
  try {
    const adminSession = await admin.context.request.get(`${admin.baseUrl}/admin/api/session`);
    expect(adminSession.ok()).toBeTruthy();
    expect((await adminSession.json()).actor.csrf_token).toBe(admin.csrf);

    const created = await userA.context.request.post(`${userA.baseUrl}/me/api/apps`, {
      data: { name: "P1B browser-created app" },
      headers: { "x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": userA.csrf },
    });
    expect(created.status()).toBe(201);
    const appId = (await created.json()).app.id;
    const denied = await userB.context.request.delete(`${userB.baseUrl}/me/api/apps/${appId}`, {
      headers: { "x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": userB.csrf },
    });
    expect(denied.status()).toBe(404);
  } finally {
    await Promise.all([admin.context.close(), userA.context.close(), userB.context.close()]);
  }
});
