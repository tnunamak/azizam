import type { APIRequestContext } from "@playwright/test";

/** Keep a seeded owner session fresh for a day.
 *
 * Risky owner actions (export, provider keys, invites, people, sign-in rules)
 * answer 401 reauth_required once the session's last password check is older
 * than the step-up window (15 minutes by default). A fixture run lasts longer
 * than that and its saved sign-in is never typed again, so every test that
 * touches such a route would hang on the confirm prompt. Call this right after
 * the sign-in or owner setup, while the session is still fresh enough to make
 * this (itself risky) PUT. Idle and absolute lifetimes keep their defaults. */
export async function widenStepUpWindow(api: APIRequestContext): Promise<void> {
  const session = await (await api.get("/admin/api/session")).json();
  const current = await api.get("/admin/api/settings/sessions");
  if (!current.ok())
    throw new Error(`Seed sessions settings returned HTTP ${current.status()}`);
  const { revision } = await current.json();
  const saved = await api.put("/admin/api/settings/sessions", {
    data: { idle_timeout_days: 30, absolute_lifetime_days: 90, step_up_minutes: 1440 },
    headers: {
      "x-azizam-csrf": session.actor.csrf_token,
      "x-azizam-admin-action": "1",
      "if-match": revision,
    },
  });
  if (!saved.ok()) throw new Error(`Seed sessions update returned HTTP ${saved.status()}`);
}
