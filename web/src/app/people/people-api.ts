import { adminFetch, type AccessPlanChoice } from "@/lib/api";

export interface Person {
  id: string;
  name: string | null;
  email: string | null;
  /** The person's access plan; null means "No access plan": they cannot generate. */
  plan_id: string | null;
  plan_name: string | null;
  grants?: { role: string; source: "manual" | "oidc-claim"; scope_app_id: string | null }[];
  disabled: boolean;
  last_active: number | null;
  requests_this_month: number | null;
  builtin?: boolean;
  protected?: boolean;
  role?: "owner" | "admin" | "member";
}

export function grantSourceLabel(source: "manual" | "oidc-claim"): string {
  return source === "oidc-claim" ? "OIDC claim" : "Manual";
}

const roleWords = { owner: "Owner", admin: "Administrator", member: "Member" };

/** A role and where it came from, as words: "Owner (manual)". */
export function grantLabel(grant: { role: string; source: "manual" | "oidc-claim" }): string {
  const role = roleWords[grant.role as keyof typeof roleWords] ?? grant.role;
  return `${role} (${grant.source === "oidc-claim" ? "OIDC claim" : "manual"})`;
}

/** Why a protected account has no actions here. */
export function protectedLabel(role: Person["role"]): string {
  return role === "owner" ? "Owner" : role === "admin" ? "Administrator" : "Protected account";
}
export interface Invite {
  id: string;
  email: string | null;
  plan_id: string | null;
  plan_name: string | null;
  expires_at: number;
  created_at: number;
  used_at: number | null;
  revoked_at: number | null;
}
export const peopleApi = {
  people: () =>
    adminFetch<{ people: Person[]; access_plans: AccessPlanChoice[] }>("/admin/api/people"),
  invites: () =>
    adminFetch<{
      invites: Invite[];
      access_plans: AccessPlanChoice[];
      /** Preselected for new invitations; null: "Ask each time". */
      invite_access_plan: string | null;
    }>("/admin/api/invites"),
  /** ``access_plan: null`` sets "No access plan". */
  change: (id: string, body: { access_plan?: string | null; disabled?: boolean }) =>
    adminFetch(`/admin/api/people/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
    }),
  remove: (id: string) =>
    adminFetch(`/admin/api/people/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  reset: (id: string) =>
    adminFetch<{ url: string; expires_at: number }>(
      `/admin/api/people/${encodeURIComponent(id)}/reset`,
      { method: "POST", body: { expires_in: 3600 } },
    ),
  invite: (body: { access_plan: string; email?: string; expires_in: number }) =>
    adminFetch<{ invite: Invite; url: string }>("/admin/api/invites", {
      method: "POST",
      body,
    }),
  revoke: (id: string) =>
    adminFetch(`/admin/api/invites/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  inviteLink: (id: string) =>
    adminFetch<{ url: string }>(
      `/admin/api/invites/${encodeURIComponent(id)}/link`,
    ),
};
