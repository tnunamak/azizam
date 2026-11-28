// The editable copy of the sign-in settings and the small pure rules around it.
// None of this decides who gets in. That is the server's evaluator; the page
// only shapes what the owner typed into the request the server expects.
import type {
  ProviderConnection,
  SignInProposal,
  SignInRule,
  SignInSnapshot,
} from "@/lib/signin-api";
import { DEFAULTS, isComplete } from "./copy";

/** What the owner is editing. The same shape the preview and save routes take. */
export type Form = SignInProposal;

export function formFromSnapshot(s: SignInSnapshot): Form {
  const p = s.provider;
  return {
    methods: { ...s.methods },
    provider: {
      name: p.name,
      address: p.address,
      client_id: p.client_id,
      client_secret: "",
      scopes: p.scopes,
      groups_claim: p.groups_claim,
      authorization_endpoint: p.authorization_endpoint,
      token_endpoint: p.token_endpoint,
      jwks_uri: p.jwks_uri,
      clock_skew_seconds: p.clock_skew_seconds,
    },
    rules: {
      fallback: s.rules.fallback,
      rules: s.rules.rules.map((r) => ({ ...r, values: [...r.values] })),
    },
    memory: s.memory ?? "remember",
  };
}

/** Changes whenever the connection changes in a way that needs a new test. */
export function connectionSig(c: ProviderConnection): string {
  return JSON.stringify([
    c.address.trim(),
    c.client_id.trim(),
    c.client_secret ? "new" : "",
    c.scopes.trim(),
    c.groups_claim.trim(),
    c.authorization_endpoint.trim(),
    c.token_endpoint.trim(),
    c.jwks_uri.trim(),
    c.clock_skew_seconds,
  ]);
}

/** Changes whenever anything the owner can save changes. Unfinished rules do not count. */
export function formSig(f: Form): string {
  return JSON.stringify([
    f.methods.password,
    f.methods.provider,
    f.rules.fallback,
    f.rules.rules
      .filter(isComplete)
      .map((r) => [r.id, r.field, r.values, r.admit || r.admin, r.admin, r.plan]),
    f.provider.name.trim(),
    connectionSig(f.provider),
    f.memory ?? "remember",
  ]);
}

export function newRule(id: string): SignInRule {
  return { id, field: "group", values: [], admit: false, admin: false, plan: null };
}

export const planRules = (rules: SignInRule[]) => rules.filter((r) => r.plan);

/** Swap a plan rule with its neighbour among the plan rules. */
export function moveRule(
  rules: SignInRule[],
  id: string,
  direction: "up" | "down",
): SignInRule[] {
  const plan = planRules(rules);
  const at = plan.findIndex((r) => r.id === id);
  const other = plan[direction === "up" ? at - 1 : at + 1];
  if (at < 0 || !other) return rules;
  const next = [...rules];
  const i = rules.findIndex((r) => r.id === id);
  const j = rules.findIndex((r) => r.id === other.id);
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

export function sameRule(a: SignInRule, b: SignInRule): boolean {
  return (
    a.field === b.field &&
    a.admit === b.admit &&
    a.admin === b.admin &&
    a.plan === b.plan &&
    a.values.length === b.values.length &&
    a.values.every((v, i) => v === b.values[i])
  );
}

export const hasSavedConnection = (s: SignInSnapshot) =>
  Boolean(s.provider.address && s.provider.client_id);

/** The five Advanced fields, with where each default comes from. */
export type AdvancedKey =
  | "scopes"
  | "groups_claim"
  | "authorization_endpoint"
  | "token_endpoint"
  | "jwks_uri"
  | "clock_skew_seconds";

export const FIELD_DEFAULT: Partial<Record<AdvancedKey, string | number>> = {
  scopes: DEFAULTS.scopes,
  groups_claim: DEFAULTS.groupsClaim,
  clock_skew_seconds: DEFAULTS.clockSkewSeconds,
};
