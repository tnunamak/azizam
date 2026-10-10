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
  plan_source?: "assigned" | "email" | "domain" | "group" | "role" | "gateway_default";
  grants?: { role: string; source: "manual" | "oidc-claim"; scope_app_id: string | null }[];
  disabled: boolean;
  last_active: number | null;
  requests_this_month: number | null;
  builtin?: boolean;
  protected?: boolean;
  role?: "owner" | "admin" | "member";
}

export function planSourceLabel(source: Person["plan_source"]): string {
  return { assigned: "Assigned", email: "Email mapping", domain: "Domain mapping",
    group: "Group mapping", role: "Role mapping", gateway_default: "Gateway default" }[source ?? "gateway_default"];
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
  inviteLink: (id: string) =>
    adminFetch<{ url: string }>(
      `/admin/api/invites/${encodeURIComponent(id)}/link`,
    ),
};

export function planSummary(policy: ProfilePolicyDocument): string {
  const parts: string[] = [];
  const intents = policy.allow?.intents;
  const labels: Record<string, string> = {
    chat: "chat",
    "chat.*": "chat",
    "chat.completions": "chat",
    images: "images",
    image: "images",
    "image.*": "images",
    "image.generate": "image generation",
    "image.edit": "image editing",
    video: "video",
    "video.*": "video",
    "video.generate": "video generation",
    voice: "speech",
    "speech.synthesis": "speech",
    "audio.generate": "audio generation",
    transcribe: "transcription",
    "speech.transcription": "transcription",
    "speech.transcription.*": "transcription",
    "speech.translation": "translation",
    decide: "decisions",
    decisions: "decisions",
    "text.decisions": "decisions",
    "text.systemone": "decisions",
    embed: "embeddings",
    "text.embeddings": "embeddings",
    "text.rerank": "reranking",
    "*": "all capabilities",
  };
  const capabilities = [
    ...new Set(
      (intents || []).map(
        (value) => labels[value] ?? value.replace(/[._-]/g, " "),
      ),
    ),
  ];
  const capabilitySentence = new Intl.ListFormat("en", {
    style: "long",
    type: "conjunction",
  }).format(capabilities);
  parts.push(
    intents === undefined
      ? "Capabilities follow gateway defaults"
      : intents.length
        ? capabilitySentence.charAt(0).toUpperCase() +
          capabilitySentence.slice(1)
        : "No capabilities allowed",
  );
  const exposure = policy.allow?.exposure;
  const localOnly = exposure?.length === 1 && exposure[0] === "local";
  if (exposure === undefined)
    parts.push("Model locations follow gateway defaults");
  else if (!exposure.length) parts.push("No model locations allowed");
  else if (localOnly) parts.push("Local models only");
  else if (exposure.length === 1 && exposure[0] === "remote")
    parts.push("Remote models only");
  else if (
    exposure.length === 2 &&
    exposure.includes("local") &&
    exposure.includes("remote")
  )
    parts.push("Local and remote models");
  else parts.push(`Model locations: ${exposure.join(", ")}`);

  // The policy parser defaults remote.providers to an empty set. Cloud fallback
  // is a separate permission; it never grants access to a paid provider.
  const providers = policy.remote?.providers ?? [];
  if (localOnly || !providers.length) parts.push("No paid providers");
  else
    parts.push(
      `Paid providers: ${new Intl.ListFormat("en", { type: "conjunction" }).format(providers)}`,
    );
  if (!localOnly)
    parts.push(
      policy.fallback?.cloud_allowed
        ? "Cloud fallback permitted"
        : "No cloud fallback",
    );
  if (policy.models !== undefined)
    parts.push(
      policy.models.length ? "Selected models only" : "No models allowed",
    );
  if (policy.protocols !== undefined)
    parts.push(
      policy.protocols.length
        ? "Selected app protocols only"
        : "No app protocols allowed",
    );
  if (policy.workflows !== undefined)
    parts.push(
      policy.workflows.length
        ? "Selected workflows only"
        : "No workflows allowed",
    );
  if (policy.backends !== undefined)
    parts.push(
      policy.backends.length ? "Selected engines only" : "No engines allowed",
    );
  if (
    [
      policy.allow?.trust_tiers,
      policy.allow?.resource_tiers,
      policy.allow?.tags,
    ].some((values) => values !== undefined)
  )
    parts.push("Additional capability restrictions apply");
  for (const [value, period] of [
    [policy.remote?.max_usd_per_request, "per request"],
    [policy.remote?.budget_usd_per_day, "per day"],
    [policy.remote?.budget_usd_per_month, "per month"],
  ] as const) {
    if (value !== null && value !== undefined)
      parts.push(`Remote spending limit: $${value} ${period}`);
  }
  return `${parts.join(". ")}.`;
}

/**
 * What an invitation gives the person accepting it, in plain words. The
 * owner's planSummary names every policy field ("Capabilities follow gateway
 * defaults", "Selected app protocols only"); an invitee needs only what they
 * will be able to do and whether it can cost money. A field the plan leaves
 * at the gateway's defaults says nothing.
 */
export function inviteeSummary(policy: ProfilePolicyDocument): string {
  const parts: string[] = [];
  const intents = policy.allow?.intents;
  if (intents !== undefined && !intents.includes("*")) {
    const tasks = [
      ...new Set(
        intents.flatMap((intent) => {
          const family = intent.split(".")[0];
          const word = INVITEE_TASKS[intent] ?? INVITEE_TASKS[family];
          return word === undefined ? [] : [word];
        }),
      ),
    ].filter(Boolean);
    parts.push(
      tasks.length
        ? `You can ${new Intl.ListFormat("en", { type: "conjunction" }).format(tasks)}`
        : "Ask the owner what you can use",
    );
  }
  const exposure = policy.allow?.exposure;
  const localOnly = exposure?.length === 1 && exposure[0] === "local";
  if (localOnly) parts.push("Everything runs on this gateway's own computer");
  const providers = policy.remote?.providers ?? [];
  const capped = [
    policy.remote?.max_usd_per_request,
    policy.remote?.budget_usd_per_day,
    policy.remote?.budget_usd_per_month,
  ].some((value) => value !== null && value !== undefined);
  if (!localOnly) {
    parts.push(
      !providers.length
        ? "No paid online services"
        : capped
          ? "Some models are paid online services, within a spending limit"
          : "Some models are paid online services",
    );
  }
  return parts.length ? `${parts.join(". ")}.` : "";
}

/** Plain verbs for the tasks a plan allows; app plumbing (embeddings, decisions) says nothing. */
const INVITEE_TASKS: Record<string, string> = {
  chat: "chat",
  image: "make and edit pictures",
  images: "make and edit pictures",
  "image.generate": "make pictures",
  "image.edit": "edit pictures",
  video: "make videos",
  voice: "hear text read aloud",
  speech: "hear text read aloud",
  "speech.synthesis": "hear text read aloud",
  "speech.transcription": "turn recordings into text",
  transcribe: "turn recordings into text",
  audio: "make music and sounds",
  "audio.generate": "make music and sounds",
  text: "",
  decide: "",
  decisions: "",
  embed: "",
};

export function planCounts(policy: ProfilePolicyDocument): string {
  return (
    [
      [policy.models, "model"],
      [policy.protocols, "protocol"],
      [policy.workflows, "workflow"],
      [policy.backends, "engine"],
    ] as const
  )
    .filter(([values]) => values !== undefined)
    .map(
      ([values, label]) =>
        `${values!.length} selected ${label}${values!.length === 1 ? "" : "s"}`,
    )
    .join(" · ");
}
