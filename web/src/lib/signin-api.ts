// Contract for Settings > Sign-in. The routes are owner routes; an
// administrator may read the snapshot (GET /admin/api/signin) and nothing else.
// The server (gateway/signin_api.py) owns every rule of who gets in. The page
// shows what the server computed and never re-implements the evaluator:
// preview, test verdict and enforcement are one function on the server.
import { adminFetch } from "./api";

export type RuleField = "group" | "role" | "email" | "domain" | "everyone";

export interface SignInRule {
  /** Stable id. A new rule gets any client id such as "new-1"; the server keeps it if valid. */
  id: string;
  field: RuleField;
  /** Empty for "everyone". Several values mean "is one of". */
  values: string[];
  /** Let in. True whenever `admin` is true (administrators are always let in). */
  admit: boolean;
  admin: boolean;
  /** Access plan id given by this rule, or null. The first rule in list order that gives a plan wins. */
  plan: string | null;
}

export interface SignInRules {
  /** closed: kept out unless a rule lets them in or you invited them. open: let in with no plan. */
  fallback: "closed" | "open";
  /** List order is the plan priority. */
  rules: SignInRule[];
}

export interface SeenValue {
  value: string;
  /** People who have signed in with the value. 0 with `note` set for values only seen in a test. */
  count: number;
  /** Shown instead of the count, for example "Simon Agent" (emails) or "seen in a test". */
  note?: string;
}

export interface PlanRef {
  id: string;
  name: string;
}

/** What the owner types. Never the saved secret: a blank `client_secret` keeps it. */
export interface ProviderConnection {
  /** The typed Name. Blank is allowed: the effective name is then the host of `address`. */
  name: string;
  /** The provider address (the OIDC issuer). */
  address: string;
  client_id: string;
  client_secret: string;
  scopes: string;
  groups_claim: string;
  /** Blank means "use what discovery finds". */
  authorization_endpoint: string;
  token_endpoint: string;
  jwks_uri: string;
  clock_skew_seconds: number;
}

export interface SignInSnapshot {
  /** Send back in If-Match on PUT. */
  revision: string;
  viewer: { role: "owner" | "admin"; can_edit: boolean };
  methods: { password: boolean; provider: boolean };
  provider: Omit<ProviderConnection, "client_secret"> & {
    client_secret_saved: boolean;
    /** Host of `address`, "" when there is none. */
    host: string;
    /** Typed name, else host, else "". */
    effective_name: string;
    /** The first-party callback address to copy into the provider. */
    redirect_uri: string;
    /** Seconds since epoch of the last passed test of the saved connection, or null. */
    tested_at: number | null;
    /** The last passed test saw no groups claim. */
    groups_missing: boolean;
  };
  rules: SignInRules;
  /** By rule id. Names are empty for an administrator. */
  rule_matches: Record<string, { count: number; names: string[] }>;
  seen: Record<"group" | "role" | "email" | "domain", SeenValue[]>;
  plans: PlanRef[];
  owner: { name: string; email: string; has_password: boolean };
  /** The command the owner runs on the gateway host to get back in. */
  recovery_command: string;
  /** A one-time note from the upgrade. Dismissed with dismissNotice. */
  notice: "removed_owner_rule" | null;
}

export interface SignInProposal {
  methods: { password: boolean; provider: boolean };
  provider: ProviderConnection;
  rules: SignInRules;
}

export interface PreviewPerson {
  id: string;
  name: string;
  /** Can they sign in at all, and the way they do (a password or the provider). */
  sign_in: { before: boolean; after: boolean; via: "password" | "provider" };
  admin: { before: boolean; after: boolean };
  plan: { before: PlanRef | null; after: PlanRef | null };
  /** Admin keys this person made. Revoked on save when `admin` goes true to false, unless kept. */
  admin_keys: number;
}

export type PreviewSetting =
  | "password_on"
  | "password_off"
  | "provider_on"
  | "provider_off"
  | "fallback_closed"
  | "fallback_open"
  | "name_changed"
  | "connection_changed";

export type TestOutcome = {
  admitted: boolean;
  admin: boolean;
  plan: PlanRef | null;
  /** Rule ids that matched, in list order. */
  rule_ids: string[];
  /** True when the only reason they get in is an invitation. */
  invited: boolean;
};

export type TestVerdict =
  | { kind: "owner_linked" }
  | { kind: "owner_unlinked"; email: string; old_link_stale: boolean }
  | { kind: "person"; person: PlanRef; outcome: TestOutcome }
  | {
      kind: "new";
      email: string;
      host: string;
      issuer_changed: boolean;
      outcome: TestOutcome;
    };

export interface Preview {
  /** Only people whose sign-in, administrator status or plan changes. */
  people: PreviewPerson[];
  /** Names of people who have signed in before and are not affected. */
  unaffected: string[];
  /** Set only when the address changes and the provider stays on. */
  issuer_change: { old_host: string; new_host: string; people: number } | null;
  settings: PreviewSetting[];
  /** Why Save cannot run. Empty when it can. */
  blockers: {
    code:
      | "both_methods_off"
      | "test_required"
      | "address_invalid"
      | "incomplete_rule"
      | "owner_not_linked";
    message: string;
  }[];
  /** Present when `test_id` was sent and that test passed. */
  test_verdict: TestVerdict | null;
}

export type TestFailureCause =
  | "redirect_not_allowed"
  | "wrong_secret"
  | "unverified_email"
  | "unreachable"
  | "refused";

export type TestStatus =
  | { status: "waiting" }
  | {
      status: "passed";
      tested_at: number;
      identity: {
        name: string;
        email: string;
        email_verified: boolean;
        groups: string[];
        roles: string[];
        /** The names of the claims the provider sent. */
        claims: string[];
        groups_claim: string;
      };
      /** "no_groups": sign-in worked but the groups claim was missing. */
      warnings: "no_groups"[];
      /** True when this identity is the owner's and is not linked yet. */
      can_link: boolean;
      details: string;
    }
  | {
      status: "failed";
      cause: TestFailureCause;
      /** Plain words for the owner, already naming the provider. */
      message: string;
      details: string;
    };

export interface SaveRequest {
  proposal: SignInProposal;
  /** The test whose result allows turning the provider on or keeping it on. */
  test_id: string | null;
  /** Person ids whose admin keys stay working although they lose administrator. */
  keep_admin_keys: string[];
  /** Save the connection with the provider off. Allowed without a passed test. */
  save_for_later: boolean;
}

export interface SaveResult {
  snapshot: SignInSnapshot;
  applied: {
    provider_turned_on: boolean;
    /** People who lost administrator, with what happened to their admin keys. */
    admin_removed: { name: string; keys_revoked: number; keys_kept: number }[];
  };
}

export interface Discovery {
  ok: boolean;
  host: string;
  authorization_endpoint: string;
  token_endpoint: string;
  jwks_uri: string;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export const signInKeys = { snapshot: ["settings", "signin"] as const };

export const getSignIn = () => adminFetch<SignInSnapshot>("/admin/api/signin");

export const discoverProvider = (address: string) =>
  adminFetch<Discovery>("/admin/api/signin/discover", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ address }),
  });

export const previewSignIn = (proposal: SignInProposal, testId: string | null) =>
  adminFetch<Preview>("/admin/api/signin/preview", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ proposal, test_id: testId }),
  });

export const startSignInTest = (provider: ProviderConnection) =>
  adminFetch<{ test_id: string; start_url: string }>(
    "/admin/api/signin/test",
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ provider }),
    },
  );

export const getSignInTest = (testId: string) =>
  adminFetch<TestStatus>(`/admin/api/signin/test/${encodeURIComponent(testId)}`);

export const cancelSignInTest = (testId: string) =>
  adminFetch<void>(`/admin/api/signin/test/${encodeURIComponent(testId)}`, {
    method: "DELETE",
  });

export const linkOwner = (testId: string) =>
  adminFetch<SignInSnapshot>("/admin/api/signin/link-owner", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ test_id: testId }),
  });

export const saveSignIn = (revision: string, request: SaveRequest) =>
  adminFetch<SaveResult>("/admin/api/signin", {
    method: "PUT",
    headers: { ...JSON_HEADERS, "If-Match": revision },
    body: JSON.stringify(request),
  });

export const dismissNotice = () =>
  adminFetch<SignInSnapshot>("/admin/api/signin/notice/dismiss", {
    method: "POST",
  });

export const setOwnerPassword = (password: string) =>
  adminFetch<SignInSnapshot>("/admin/api/signin/owner-password", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ password }),
  });
