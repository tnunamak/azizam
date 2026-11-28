import type { AccessPlan, App } from "@/lib/api";

export interface DisconnectImpact {
  plans: { id: string; name: string }[];
  apps: { id: string; name: string }[];
}

/**
 * Who a disconnect reaches: the access plans that list this connection, and
 * the apps whose effective access plan is one of them. Both come from reads
 * the page already makes; nothing is guessed when a read is missing.
 */
export function disconnectImpact(
  connectionId: string | undefined,
  plans: readonly AccessPlan[],
  apps: readonly App[],
): DisconnectImpact {
  if (!connectionId) return { plans: [], apps: [] };
  const using = plans.filter((plan) => plan.plan.connections?.includes(connectionId));
  const ids = new Set(using.map((plan) => plan.id));
  return {
    plans: using.map((plan) => ({ id: plan.id, name: plan.name })),
    apps: apps
      .filter((app) => app.disabled_at === null && app.effective_access_plan_id !== null && ids.has(app.effective_access_plan_id))
      .map((app) => ({ id: app.id, name: app.name })),
  };
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "2 access plans (Household, Local chat) and 3 apps use it." or "No access plan uses it." */
export function impactSentence(impact: DisconnectImpact): string {
  if (impact.plans.length === 0) return "No access plan uses it.";
  const names = impact.plans.map((plan) => plan.name).join(", ");
  const apps = impact.apps.length === 0 ? "no apps" : count(impact.apps.length, "app", "apps");
  return `${count(impact.plans.length, "access plan", "access plans")} (${names}) and ${apps} use it.`;
}
