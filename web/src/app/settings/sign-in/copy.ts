// Every sentence on the Sign-in page. Pure functions, no React, so the wording
// rules can be tested on their own.
//
// The name rule (critique round 3, N3): the provider's display name is the
// typed Name, else the host of the provider address. It is the same in every
// string. The words "your provider" appear only before an address exists, and
// no sentence starts with a bare host (a host is never capitalised).
import type {
  PlanRef,
  Preview,
  PreviewPerson,
  PreviewSetting,
  ProviderConnection,
  RuleField,
  SignInRule,
  TestFailureCause,
  TestOutcome,
  TestVerdict,
} from "@/lib/signin-api";

export const FIELD_LABEL: Record<RuleField, string> = {
  group: "Group",
  role: "Role",
  email: "Email",
  domain: "Email domain",
  everyone: "Everyone",
};

/** What discovery finds when the provider says nothing else. */
export const DEFAULTS = {
  scopes: "openid email profile",
  groupsClaim: "groups",
  clockSkewSeconds: 60,
};

export function hostOf(address: string): string {
  try {
    return new URL(address.trim()).host;
  } catch {
    return "";
  }
}

/** The two inputs of the name rule, and the result. `name` is "" before an address exists. */
export interface ProviderLabel {
  typed: string;
  host: string;
  name: string;
}

export function providerLabel(
  typedName: string,
  address: string,
): ProviderLabel {
  const typed = typedName.trim();
  const host = hostOf(address);
  return { typed, host, name: typed || host };
}

/** Mid-sentence name. "your provider" only while no address exists. */
export const mid = (p: ProviderLabel) => p.name || "your provider";
/** The row label. Never capitalised when it is a host. */
export const rowLabel = (p: ProviderLabel) => p.name || "Your provider";
/** Starts a sentence. A host does not, so it is rephrased. */
export const subject = (p: ProviderLabel) =>
  p.typed || (p.host ? `Sign-in with ${p.host}` : "Your provider");
/** Names the sign-in way itself: "Family account sign-in", "Sign-in with id.example.com". */
export const signInOf = (p: ProviderLabel) =>
  p.typed
    ? `${p.typed} sign-in`
    : p.host
      ? `Sign-in with ${p.host}`
      : "Your provider sign-in";
export const possessive = (p: ProviderLabel) =>
  p.name ? `${p.name}’s` : "its";
/** The text of the sign-in button the household sees. */
export const buttonText = (p: ProviderLabel) =>
  `Sign in with ${p.name || "…"}`;
/** Where the household's accounts live. */
export const hostOr = (p: ProviderLabel) => p.host || "your provider";

export const count = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;
export const people = (n: number) => (n === 1 ? "1 person" : `${n} people`);

/** "just now", "today at 09:14", "on Oct 12". */
export function formatWhen(epochSeconds: number, nowMs = Date.now()): string {
  const at = new Date(epochSeconds * 1000);
  const now = new Date(nowMs);
  if (Math.abs(nowMs - at.getTime()) < 60_000) return "just now";
  if (at.toDateString() === now.toDateString()) {
    const hh = String(at.getHours()).padStart(2, "0");
    const mm = String(at.getMinutes()).padStart(2, "0");
    return `today at ${hh}:${mm}`;
  }
  return `on ${at.toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
}

// ---------------------------------------------------------------- headline

export const letsIn = (r: SignInRule) => r.admit || r.admin;
export const isComplete = (r: SignInRule) =>
  (r.field === "everyone" || r.values.length > 0) &&
  Boolean(r.admit || r.admin || r.plan);

export function headline(a: {
  provider: boolean;
  password: boolean;
  hasConnection: boolean;
  fallback: "closed" | "open";
  rules: SignInRule[];
  label: ProviderLabel;
}): string {
  const { label: p } = a;
  const n = mid(p);
  if (!a.provider)
    return a.hasConnection
      ? `People sign in with a password. ${subject(p)} is saved but off.`
      : "People sign in with a password. To add another way to sign in, connect a provider below.";
  const named: string[] = [];
  for (const r of a.rules.filter(isComplete).filter(letsIn))
    if (r.field === "everyone") named.push("everyone");
    else named.push(...r.values);
  let t = a.password
    ? `People sign in with a password or with ${n}. `
    : `People sign in with ${n}. You can still use your password. `;
  if (a.fallback === "closed") {
    if (!named.length) t += `Only people you invite can sign in with ${n}.`;
    else if (named.length <= 2)
      t += `${subject(p)} lets in ${named.join(" and ")}, and people you invite. Everyone else is kept out.`;
    else
      t += `${subject(p)} lets in the people your rules name, and people you invite. Everyone else is kept out.`;
  } else {
    if (named.length && named.length <= 2)
      t += `${subject(p)} lets in ${named.join(" and ")}. `;
    t += `Anyone else from ${n} gets in but can use nothing until you give them a plan.`;
  }
  return t;
}

// ------------------------------------------------------------ ways to sign in

export type Status = { tone: "ok" | "warn" | "off"; text: string };

export function providerStatus(a: {
  formOn: boolean;
  tested: boolean;
  liveUnchanged: boolean;
  /** Off, and the saved connection passed its last test. */
  savedTested: boolean;
  testedAt: number | null;
  liveBefore: boolean;
  hasConnection: boolean;
}): Status {
  if (a.formOn)
    return a.tested || a.liveUnchanged
      ? {
          tone: "ok",
          text: a.testedAt ? `On, tested ${formatWhen(a.testedAt)}` : "On",
        }
      : { tone: "warn", text: "Changed, not tested" };
  if (a.liveBefore || (a.savedTested && !a.tested))
    return { tone: "off", text: "Off" };
  if (a.tested) return { tone: "ok", text: "Passed, not on yet" };
  if (a.hasConnection) return { tone: "off", text: "Off, not tested" };
  return { tone: "warn", text: "Needs setup" };
}

export const lockedReasonPassword = (p: ProviderLabel) =>
  `Stays on until ${mid(p)} is on, so you can still sign in.`;
export const lockedReasonProvider =
  "Stays on until email and password is on, so you can still sign in.";

export const providerRowDescription = (a: {
  hasAddress: boolean;
  p: ProviderLabel;
}) =>
  a.hasAddress
    ? `Sign in with the account people already have at ${hostOr(a.p)}.`
    : "Finish the steps below, then test the sign-in.";

export const noPasswordText = (p: ProviderLabel) =>
  `You have no password yet. Set one so you can still get in when ${mid(p)} is down.`;
export const hasPasswordText = (p: ProviderLabel) =>
  `Your password always works, even when ${mid(p)} is down`;

// ------------------------------------------------------------------ connection

export const connectionIntro = (p: ProviderLabel) =>
  `Copy the first address into ${mid(p)}. Then paste ${possessive(p)} address below.`;

export const nameHelp = (p: ProviderLabel) =>
  `Shown on the sign-in page as “Sign in with ${p.typed || p.host || "…"}”.` +
  (p.typed || !p.host ? "" : " Type a friendlier name if you like.");

// ---------------------------------------------------------------- test sign-in

export function testCaption(a: {
  waiting: boolean;
  addressBad: boolean;
  ready: boolean;
  tested: boolean;
  testedWhen: string;
  changedSinceLastTest: boolean;
  p: ProviderLabel;
}): string {
  const n = mid(a.p);
  if (a.waiting)
    return `Finish signing in at ${n} in the new window. Waiting up to 5 minutes.`;
  if (a.addressBad) return "Fix the provider address first.";
  if (!a.ready) return "Fill in the fields above first.";
  if (a.tested) return `Passed ${a.testedWhen}. Opens ${n} in a new window.`;
  if (a.changedSinceLastTest)
    return `Changed since the last test. Opens ${n} in a new window.`;
  return `Sign in once at ${n} to check that it works. Opens a new window.`;
}

export type LocalCause = "blocked" | "closed" | "timeout" | "start_failed";

export function causeTitle(
  cause: TestFailureCause | LocalCause,
  p: ProviderLabel,
): string {
  switch (cause) {
    case "redirect_not_allowed":
      return "Redirect address refused";
    case "wrong_secret":
      return "Client secret not accepted";
    case "unverified_email":
      return "Email not verified";
    case "unreachable":
      return "Provider not reachable";
    case "refused":
      return "Sign-in refused";
    case "blocked":
      return "Pop-up blocked";
    case "closed":
      return "Sign-in window closed";
    case "timeout":
      return `No answer from ${mid(p)}`;
    case "start_failed":
      return "Test could not start";
  }
}

export function localCauseText(cause: LocalCause, p: ProviderLabel): string {
  switch (cause) {
    case "blocked":
      return "Your browser blocked the new window. Allow pop-ups for this page, then test again.";
    case "closed":
      return `The sign-in window closed before it finished. Test again and finish signing in at ${mid(p)}.`;
    case "timeout":
      return `Nothing came back after 5 minutes. Test again and finish signing in at ${mid(p)}.`;
    case "start_failed":
      return "Azizam could not start the test. Check the fields above and try again.";
  }
}

export const localCauseDetails: Record<LocalCause, string> = {
  blocked: "window.open returned null",
  closed: "window closed before the redirect came back",
  timeout: "no redirect received within 300 s",
  start_failed: "The test did not start.",
};

export const noGroupsTitle = "Sign-in worked, but no groups arrived";
export const noGroupsText = (p: ProviderLabel) =>
  `${subject(p)} sent no groups. Rules that use groups will match nobody. Add a groups mapper to the client in ${mid(p)}.`;
export const noGroupsRuleNote = (p: ProviderLabel, since: string) =>
  `${subject(p)} has not sent groups since ${since}. Group rules match nobody.`;

export const testWorks = (p: ProviderLabel) => `${subject(p)} works.`;

/** The rule as one line: "Group is /Trusted → let in, administrator". */
export function ruleText(r: SignInRule, plans: PlanRef[]): string {
  const subjectText =
    r.field === "everyone"
      ? "Everyone"
      : `${FIELD_LABEL[r.field]}${r.values.length > 1 ? " is one of " : " is "}${r.values.join(", ")}`;
  const effects: string[] = [];
  if (letsIn(r)) effects.push("let in");
  if (r.admin) effects.push("administrator");
  if (r.plan)
    effects.push(`plan ${plans.find((x) => x.id === r.plan)?.name ?? "chosen"}`);
  return `${subjectText} → ${effects.join(", ")}`;
}

function outcomeText(who: string, o: TestOutcome): string {
  if (!o.admitted)
    return `${who} would be kept out. No rule lets them in, and you keep out people you have not invited.`;
  return `${who} would be let in${o.admin ? " as an administrator" : ""}, with ${o.plan ? `plan ${o.plan.name}` : "no plan"}.`;
}

export interface VerdictCopy {
  big: string;
  why?: string;
  /** The Link button text, when the owner can link this identity. */
  link?: string;
  /** The look: the plain card, or the tinted one. */
  plain: boolean;
}

export function verdictCopy(
  v: TestVerdict,
  a: {
    p: ProviderLabel;
    rules: SignInRule[];
    plans: PlanRef[];
    justLinked: boolean;
  },
): VerdictCopy {
  const n = mid(a.p);
  switch (v.kind) {
    case "owner_linked":
      return a.justLinked
        ? {
            big: `Linked. You can sign in with ${n} as the owner.`,
            plain: false,
          }
        : { big: "You are the owner. You can always sign in.", plain: true };
    case "owner_unlinked":
      return {
        big: `${v.email} at ${n} is not linked to you yet.`,
        why:
          `Link it so you can sign in with ${n} as the owner.` +
          (v.old_link_stale
            ? " Your old link belonged to the old provider address."
            : ""),
        link: `Link ${v.email} to my account`,
        plain: false,
      };
    case "person":
      return {
        big: `This is ${v.person.name}’s account. ${outcomeText(v.person.name, v.outcome)}`,
        why: becauseText(v.outcome, a.rules, a.plans),
        plain: false,
      };
    case "new":
      return {
        big: `${v.email} has not signed in ${v.issuer_changed ? `at ${v.host}` : "before"}. ${outcomeText("They", v.outcome)}`,
        why: becauseText(v.outcome, a.rules, a.plans),
        plain: false,
      };
  }
}

function becauseText(
  o: TestOutcome,
  rules: SignInRule[],
  plans: PlanRef[],
): string | undefined {
  if (!o.admitted) return undefined;
  const matched = o.rule_ids
    .map((id) => rules.find((r) => r.id === id))
    .filter((r): r is SignInRule => Boolean(r));
  if (matched.length)
    return `Because: ${matched.map((r) => ruleText(r, plans)).join("; ")}.`;
  if (o.invited) return "Because you invited them.";
  return undefined;
}

// ----------------------------------------------------------------- rules

export const fieldNoun = (f: Exclude<RuleField, "everyone">) =>
  ({ group: "group", role: "role", email: "email", domain: "domain" })[f];

export const pickerLabel = (f: Exclude<RuleField, "everyone">) =>
  `Choose ${f === "domain" ? "domains" : `${fieldNoun(f)}s`}`;

export const pickerPlaceholder = (
  f: Exclude<RuleField, "everyone">,
  short: boolean,
) =>
  short
    ? {
        group: "Add a group…",
        role: "Add a role…",
        email: "Add an email…",
        domain: "Add a domain…",
      }[f]
    : {
        group: "Add a group nobody has signed in with…",
        role: "Add a role nobody has signed in with…",
        email: "Add an email…",
        domain: "Add a domain…",
      }[f];

export const noneSeen = (f: Exclude<RuleField, "everyone">) =>
  `Nobody has signed in with a ${fieldNoun(f)} yet.`;

export function adminNote(r: SignInRule): string {
  const who =
    r.field === "group"
      ? r.values.length === 1
        ? `Everyone in ${r.values[0]}`
        : "Everyone in these groups"
      : r.field === "email"
        ? r.values.length === 1
          ? "This person"
          : "These people"
        : "Everyone this rule matches";
  return `${who} can manage models, apps, keys and plans. They cannot manage people or sign-in, and cannot become the owner.`;
}

export const everyoneAdminWarning = (p: ProviderLabel) =>
  `Anyone who signs in with ${mid(p)} becomes an administrator, including people who have not signed in yet.`;
export const openFallbackWarning = (p: ProviderLabel) =>
  `Anyone with an account at ${hostOr(p)} can sign in. They can use nothing until you give them a plan.`;
export const onlyInvitedNote = (p: ProviderLabel) =>
  `Only people you invite can sign in with ${mid(p)}.`;

// ---------------------------------------------------------------- what changes

export type ChangePart = { tone: "neg" | "pos" | "plain" | "arrow"; text: string };

export interface PersonChange {
  id: string;
  name: string;
  /** One clause per change; the page joins them with commas. */
  clauses: ChangePart[][];
  /** Admin keys this person made, when they lose administrator. */
  keys: number;
}

export function personChange(
  person: PreviewPerson,
  p: ProviderLabel,
): PersonChange | null {
  const clauses: ChangePart[][] = [];
  const { sign_in: s, admin: a, plan } = person;
  if (s.before && !s.after)
    clauses.push([
      { tone: "neg", text: "can no longer sign in" },
      {
        tone: "arrow",
        text: `(uses ${s.via === "password" ? "a password" : mid(p)})`,
      },
    ]);
  if (!s.before && s.after) clauses.push([{ tone: "pos", text: "can sign in" }]);
  const lostAdmin = a.before && !a.after;
  if (lostAdmin)
    clauses.push([
      { tone: "neg", text: "loses administrator now and is signed out" },
    ]);
  if (!a.before && a.after)
    clauses.push([{ tone: "pos", text: "becomes an administrator" }]);
  if (s.after && plan.before?.id !== plan.after?.id) {
    if (!plan.before && plan.after)
      clauses.push([
        { tone: "pos", text: `gets plan ${plan.after.name}` },
        { tone: "arrow", text: "(now: no plan)" },
      ]);
    else if (plan.before && !plan.after)
      clauses.push([{ tone: "neg", text: `loses plan ${plan.before.name}` }]);
    else if (plan.before && plan.after)
      clauses.push([
        { tone: "plain", text: `plan ${plan.before.name}` },
        { tone: "arrow", text: "→" },
        { tone: "pos", text: plan.after.name },
      ]);
  }
  if (!clauses.length) return null;
  return {
    id: person.id,
    name: person.name,
    clauses,
    keys: lostAdmin ? person.admin_keys : 0,
  };
}

export const keysLine = (name: string, keys: number, keep: boolean) =>
  `${count(keys, "admin key")} ${name} made. ${keep ? "They keep working." : "They are revoked when you save."}`;

export const issuerLine = (
  i: NonNullable<Preview["issuer_change"]>,
): { who: string; what: string; rest: string } => ({
  who: `Everyone who signed in at ${i.old_host} (${people(i.people)}):`,
  what: `become new people at ${i.new_host}`,
  rest: ". Their roles, plans and your own link stay with the old accounts, so link yourself again after you save. They are checked by your rules again when they sign in.",
});

export function settingSentence(
  s: PreviewSetting,
  p: ProviderLabel,
): string {
  switch (s) {
    case "password_on":
      return "Email and password sign-in turns on. You keep your own password.";
    case "password_off":
      return "Email and password sign-in turns off. You keep your own password.";
    case "provider_on":
      return `${signInOf(p)} turns on.`;
    case "provider_off":
      return `${signInOf(p)} turns off.`;
    case "fallback_closed":
      return "Everyone else is now kept out unless invited.";
    case "fallback_open":
      return "Everyone else is now let in with no access.";
    case "name_changed":
      return `The sign-in button reads “Sign in with ${p.name || "…"}”.`;
    case "connection_changed":
      return `The connection settings change. ${subject(p)} keeps working with the old settings until you save.`;
  }
}

/** The number in "N changes": every line and every settings sentence shown. */
export function changeCount(preview: Preview | undefined): number {
  if (!preview) return 0;
  return (
    preview.people.length +
    (preview.issuer_change ? 1 : 0) +
    preview.settings.length
  );
}

// ------------------------------------------------------------------- footer

export const NOTHING_TO_SAVE = "Nothing to save.";
export const APPLY_NEXT = "Changes apply to the next sign-in. You stay signed in.";

export const unlinkedNote = (p: ProviderLabel) =>
  ` You are not linked yet, so your own ${p.name ? `${p.name} sign-in` : "sign-in"} would be turned away. Link first.`;
export const unlinkedNoteShort = " Not linked yet. Link first.";
export const relinkNote = " You will need to link your account again.";

export const turnOnCaption = (p: ProviderLabel) =>
  `The test passed. Turning it on adds a ${
    p.typed
      ? p.typed
      : p.host
        ? `“Sign in with ${p.host}”`
        : "sign-in"
  } button to the sign-in page.`;

export function blockerCaption(
  code: Preview["blockers"][number]["code"],
  fallbackMessage: string,
  a: { live: boolean; p: ProviderLabel },
): string {
  const keepsWorking = ` ${subject(a.p)} keeps working with the old settings until then.`;
  switch (code) {
    case "incomplete_rule":
      return "Finish or remove the new rule first.";
    case "address_invalid":
      return a.live
        ? `Fix the provider address first.${keepsWorking}`
        : "Fix the provider address first.";
    case "test_required":
      return a.live
        ? `Test sign-in again before you save.${keepsWorking}`
        : "Test sign-in first";
    case "owner_not_linked":
      return unlinkedNote(a.p).trim();
    case "both_methods_off":
      return fallbackMessage;
  }
}

/** Which blocker leads the caption. Rule problems first, then the connection. */
export function leadingBlocker(
  blockers: Preview["blockers"],
  live: boolean,
): Preview["blockers"][number] | undefined {
  const order = live
    ? ["incomplete_rule", "address_invalid", "test_required", "owner_not_linked", "both_methods_off"]
    : ["incomplete_rule", "test_required", "address_invalid", "owner_not_linked", "both_methods_off"];
  return [...blockers].sort(
    (x, y) => order.indexOf(x.code) - order.indexOf(y.code),
  )[0];
}

export function savedToast(a: {
  p: ProviderLabel;
  turnedOn: boolean;
  forLater: boolean;
  removed: { name: string; keys_revoked: number; keys_kept: number }[];
}): string {
  if (a.forLater) return `Saved. ${subject(a.p)} stays off until a test passes.`;
  if (a.turnedOn)
    return `Saved. ${subject(a.p)} is on. A sign-in button now shows on the sign-in page.`;
  if (a.removed.length) {
    const names = a.removed.map((x) => x.name).join(", ");
    const notes = a.removed
      .flatMap((x) => [
        x.keys_revoked
          ? `${count(x.keys_revoked, "admin key")} ${x.name} made ${x.keys_revoked === 1 ? "was" : "were"} revoked.`
          : "",
        x.keys_kept
          ? `${count(x.keys_kept, "admin key")} ${x.name} made still ${x.keys_kept === 1 ? "works" : "work"}.`
          : "",
      ])
      .filter(Boolean)
      .join(" ");
    return `Saved. ${names} lost administrator and ${a.removed.length === 1 ? "was" : "were"} signed out.${notes ? ` ${notes}` : ""}`;
  }
  return "Saved. Changes apply to the next sign-in.";
}

/** A connection with nothing typed yet. */
export const isBlankConnection = (c: ProviderConnection) =>
  !c.address.trim() && !c.client_id.trim();

/** "today" or "Oct 12", for "has not sent groups since …". */
export function sinceText(epochSeconds: number, nowMs = Date.now()): string {
  const at = new Date(epochSeconds * 1000);
  if (at.toDateString() === new Date(nowMs).toDateString()) return "today";
  return at.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export const CHECKING = "Checking what would change…";
export const PREVIEW_FAILED = "Could not check what would change. Try again.";
export const READ_ONLY_NOTE = "Only the owner can change sign-in.";
