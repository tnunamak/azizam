import {
  adminFetch,
  type Profile,
  type ProfilePolicyDocument,
} from "@/lib/api";

export interface Person {
  id: string;
  name: string | null;
  email: string | null;
  profile_id: string | null;
  plan_name: string | null;
  disabled: boolean;
  last_active: number | null;
  requests_this_month: number | null;
}
export interface Invite {
  id: string;
  email: string | null;
  profile_id: string;
  plan_name: string;
  expires_at: number;
  created_at: number;
  used_at: number | null;
  revoked_at: number | null;
}
export const peopleApi = {
  people: () =>
    adminFetch<{ people: Person[]; profiles: Profile[] }>("/admin/api/people"),
  invites: () =>
    adminFetch<{ invites: Invite[]; profiles: Profile[] }>(
      "/admin/api/invites",
    ),
  change: (id: string, body: { profile_id?: string; disabled?: boolean }) =>
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
  invite: (body: { profile_id: string; email?: string; expires_in: number }) =>
    adminFetch<{ invite: Invite; url: string }>("/admin/api/invites", {
      method: "POST",
      body,
    }),
  revoke: (id: string) =>
    adminFetch(`/admin/api/invites/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};

export function planSummary(policy: ProfilePolicyDocument): string {
  const parts: string[] = [];
  const intents = policy.allow?.intents;
  parts.push(
    intents === undefined
      ? "Capabilities follow gateway defaults"
      : intents.length
        ? intents.map((v) => v.replace(/[_-]/g, " ")).join(", ")
        : "No capabilities allowed",
  );
  const exposure = policy.allow?.exposure;
  if (exposure !== undefined)
    parts.push(
      exposure.length
        ? `Allowed locations: ${exposure.join(", ")}`
        : "No locations allowed",
    );
  else parts.push("Local and remote access follow gateway defaults");
  if (policy.fallback?.cloud_allowed === false)
    parts.push("Cloud fallback blocked");
  if (policy.fallback?.cloud_allowed === true)
    parts.push("Cloud fallback allowed");
  if (policy.models !== undefined)
    parts.push(
      policy.models.length
        ? `${policy.models.length} selected ${policy.models.length === 1 ? "model" : "models"}`
        : "No models allowed",
    );
  return parts.join(" · ");
}
