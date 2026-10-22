/**
 * One way to end a session in this browser (account switcher, W1; DESIGN.md 4.3-4.4 and 7,
 * SECURITY.md 3-4). Switch person, Sign out and Sign out everywhere all call `endSession`.
 *
 * `endSession` runs the steps in SECURITY.md section 3, "Leaving":
 *   1. tell the other tabs, which blank now;
 *   2. stop this tab: in-flight requests abort and `networkFetch` refuses new ones;
 *   3. show the cover;
 *   4. ask the gateway to end the session (one retry with a fresh CSRF token on a 403);
 *   5. on success, wipe this browser's storage except the allowlist, record the sign-out,
 *      and replace the page, so Back cannot show the previous person's page.
 *
 * The state lives in this page's JavaScript heap. Tabs share it through the `azizam.handoff`
 * record (state and kind only: no identity, no credential) on a BroadcastChannel, and a tab
 * without BroadcastChannel reads the same record from the `storage` event.
 */
import { navigate } from "@/lib/navigate";

export type EndKind = "switch" | "sign-out" | "sign-out-everywhere";
export type FailureReason = "no-answer" | "gateway-error";
export type OtherTabCause = "switch" | "sign-out" | "server";

export type Handoff =
  | { state: "LEAVING"; since: number; kind: EndKind; failure: FailureReason | null; tries: number }
  | { state: "SIGNED_OUT"; since: number; kind: EndKind };

export type SessionScreen =
  | { kind: "idle" }
  | { kind: "signing-out" }
  | { kind: "failed"; reason: FailureReason; tries: number; trying: boolean }
  | { kind: "other-tab"; cause: OtherTabCause };

/** What the gateway said about this browser's session. */
export interface SessionView {
  authenticated: boolean;
  /** Public per-sign-in id (`session_view_id`); null when the gateway did not send one. */
  epoch: string | null;
  /** The signed-in person's id; absent or null when the session has no person behind it (unknown). */
  person?: string | null;
}

export const HANDOFF_KEY = "azizam.handoff";
/** The class that hides the page until the check after a restore or a pending sign-out is done. */
export const CHECK_CLASS = "session-check";
const CHANNEL = "azizam-session";
const SESSION_PATH = "/me/api/session";
const ADMIN_SESSION_PATH = "/admin/api/session";
const LOGOUT_PATH = "/logout";
const SIGN_OUT_EVERYWHERE_PATH = "/auth/api/sign-out-everywhere";
const SIGN_IN_PATH = "/sign-in/";
/** No answer from the gateway within this long is "no answer" (DESIGN 4.4). */
const ANSWER_MS = 10_000;
/** Storage that survives a sign-out: the theme, and the handoff record. */
const KEEP_LOCAL = ["theme", HANDOFF_KEY];
const KEEP_SESSION = ["azizam.handoff.last"];
const END_KINDS: readonly EndKind[] = ["switch", "sign-out", "sign-out-everywhere"];
const FAILURES: readonly FailureReason[] = ["no-answer", "gateway-error"];

/** Rejects a request that the session ended before, or during, its answer. */
export class SessionEnded extends Error {
  readonly code = "session_ended";
  constructor() {
    super("This session has ended.");
    this.name = "SessionEnded";
  }
}

class GatewayStatus extends Error {
  constructor(readonly status: number) {
    super(`The gateway answered ${status}.`);
  }
}

let closed = false;
let scope = new AbortController();
let epoch: string | null = null;
let shownPerson: string | null = null;
let screen: SessionScreen = { kind: "idle" };
let kind: EndKind = "sign-out";
let tries = 0;
let attempt: Promise<void> | null = null;
let channel: BroadcastChannel | null = null;
const listeners = new Set<() => void>();
const stopHooks = new Set<() => void>();

export function sessionIsClosed(): boolean {
  return closed;
}

/** The abort signal every request of this page carries; it aborts when the session ends. */
export function sessionSignal(): AbortSignal {
  return scope.signal;
}

/** A response is current while this tab is open and the person it shows has not changed since the request began. */
export function sessionAccepts(startedEpoch: string | null): boolean {
  return !closed && (startedEpoch === null || startedEpoch === epoch);
}

export function sessionEpoch(): string | null {
  return epoch;
}

/** Runs when this tab stops using its session (query caches and the CSRF token are dropped). */
export function onSessionStopping(hook: () => void): () => void {
  stopHooks.add(hook);
  return () => stopHooks.delete(hook);
}

export function getSessionScreen(): SessionScreen {
  return screen;
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function stop(): void {
  if (closed) return;
  closed = true;
  scope.abort();
  for (const hook of stopHooks) hook();
}

function show(next: SessionScreen): void {
  screen = next;
  for (const listener of listeners) listener();
}

function setOtherTab(cause: OtherTabCause): void {
  stop();
  show({ kind: "other-tab", cause });
}

function causeOf(end: EndKind): OtherTabCause {
  return end === "switch" ? "switch" : "sign-out";
}

/** Switch person, Sign out and Sign out everywhere end the session here. A second call while this tab is already leaving is a no-op. */
export function endSession(end: EndKind): Promise<void> {
  if (closed) return attempt ?? Promise.resolve();
  kind = end;
  tries = 0;
  publish({ state: "LEAVING", since: Date.now(), kind: end, failure: null, tries: 0 });
  stop();
  show({ kind: "signing-out" });
  return runSignOut();
}

/** "Try again" on the failure screen. */
export function retrySignOut(): Promise<void> {
  if (screen.kind !== "failed") return attempt ?? Promise.resolve();
  show({ ...screen, trying: true });
  return runSignOut();
}

/** Continue on the other-tab screen: a sign-out still pending shows its screen; otherwise the start page. */
export async function continueFromOtherTab(): Promise<void> {
  const record = readHandoff();
  if (record?.state === "LEAVING") {
    mirrorLeaving(record);
    return;
  }
  const seen = await readSessionNow();
  wipeTabStorage();
  navigate.replace(seen?.authenticated ? "/" : SIGN_IN_PATH);
}

/** Boot: a sign-out another tab (or an earlier visit) left pending finishes before this page shows anything. Returns true when it took over the page. */
export function resumeSignOut(): boolean {
  const record = readHandoff();
  if (record?.state !== "LEAVING") return false;
  kind = record.kind;
  tries = record.tries;
  stop();
  show({ kind: "signing-out" });
  void runSignOut();
  return true;
}

/** A page shown from the back/forward cache, or restored after a restart: check before anything paints. */
export async function checkPageShown(): Promise<void> {
  if (resumeSignOut() || closed) return;
  const seen = await readSessionNow();
  if (closed) return;
  const record = readHandoff();
  if (seen === null) {
    if (record?.state === "SIGNED_OUT") setOtherTab(causeOf(record.kind));
    return;
  }
  if (epoch === null) return;
  if (!seen.authenticated || !sameShownPerson(seen)) {
    setOtherTab(record?.state === "SIGNED_OUT" ? causeOf(record.kind) : "server");
    return;
  }
  epoch = seen.epoch ?? epoch;
}

/** Whether a snapshot is still the person this page shows. A known person is compared as a person: the same
 * person in a new session (a step-up renews the session id) is the same person. With no person on either
 * side, only the same session id proves it. */
function sameShownPerson(seen: SessionView): boolean {
  const person = seen.person ?? null;
  if (person !== null && shownPerson !== null) return person === shownPerson;
  return seen.epoch === epoch;
}

/**
 * A live tab learns what `/me/api/session` says. A different person (a new sign-in in another
 * tab) ends this tab's view. An expired session is left to the sign-in redirect, as before.
 */
export function observeViewer(seen: SessionView): void {
  if (closed || !seen.authenticated || seen.epoch === null) return;
  if (epoch === null) {
    epoch = seen.epoch;
    shownPerson = seen.person ?? null;
    return;
  }
  if (!sameShownPerson(seen)) {
    setOtherTab("server");
    return;
  }
  // The same person in a new session: adopt the new id, and show nothing.
  epoch = seen.epoch;
}

/** A sign-in clears the record of an earlier sign-out (SECURITY.md section 3). */
export async function forgetFinishedSignOut(): Promise<void> {
  if (readHandoff()?.state !== "SIGNED_OUT") return;
  const seen = await readSessionNow();
  if (!seen?.authenticated) return;
  try {
    localStorage.removeItem(HANDOFF_KEY);
  } catch {
    /* storage blocked: the record only matters to other tabs */
  }
}

/**
 * Listens for other tabs. Call once per page; the returned function stops listening.
 * The BroadcastChannel is the primary path; the `storage` event carries the same record when
 * BroadcastChannel does not exist.
 */
export function startSessionSync(): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === HANDOFF_KEY) applyRemote(parseHandoff(event.newValue));
  };
  const onOnline = () => {
    if (screen.kind === "failed" && screen.reason === "no-answer") void retrySignOut();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener("online", onOnline);
  let own: BroadcastChannel | null = null;
  if (typeof BroadcastChannel === "function") {
    own = new BroadcastChannel(CHANNEL);
    own.onmessage = (event: MessageEvent) => applyRemote(parseHandoff(event.data));
  }
  channel = own;
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("online", onOnline);
    own?.close();
    if (channel === own) channel = null;
  };
}

/** The sign-out record in this browser, or null. */
export function readHandoff(): Handoff | null {
  try {
    return parseHandoff(localStorage.getItem(HANDOFF_KEY));
  } catch {
    return null;
  }
}

/** Runs first in the document head: a pending sign-out hides the page before React starts, and a page that is about to enter the back/forward cache hides itself. */
export const SESSION_HEAD_SCRIPT = `(function(){var c=${JSON.stringify(CHECK_CLASS)};try{var h=JSON.parse(localStorage.getItem(${JSON.stringify(HANDOFF_KEY)})||"null");if(h&&h.state==="LEAVING")document.documentElement.classList.add(c)}catch(x){}addEventListener("pagehide",function(){document.documentElement.classList.add(c)})})();`;

/** Shows the page again once the state for it is rendered. */
export function revealPage(): void {
  document.documentElement.classList.remove(CHECK_CLASS);
}

/** Wipes this browser's storage and caches for the person who is leaving. Theme and the handoff record survive. */
export async function wipeBrowserState(): Promise<void> {
  wipeTabStorage();
  await Promise.allSettled([dropDatabases(), dropCaches(), dropWorkers()]);
}

/** Session storage belongs to one tab, so every tab clears its own when the session ends here (SECURITY.md
 * section 4). The keys that survive are the same in every tab. */
function wipeTabStorage(): void {
  keepOnly(() => localStorage, KEEP_LOCAL);
  keepOnly(() => sessionStorage, KEEP_SESSION);
}

/** Test-only: forget this page's state between specs. */
export function resetSessionStateForTests(): void {
  closed = false;
  scope = new AbortController();
  epoch = null;
  kind = "sign-out";
  tries = 0;
  attempt = null;
  channel = null;
  show({ kind: "idle" });
}

function runSignOut(): Promise<void> {
  if (!attempt) {
    attempt = gatewayAnswer(kind)
      .then(async (answer) => {
        if (answer.ended) {
          await finish(answer.next);
          return;
        }
        tries += 1;
        publish({ state: "LEAVING", since: Date.now(), kind, failure: answer.reason, tries });
        show({ kind: "failed", reason: answer.reason, tries, trying: false });
      })
      .finally(() => {
        attempt = null;
      });
  }
  return attempt;
}

async function finish(next: string): Promise<void> {
  // The page is leaving: a retry that succeeded shows the cover, not its "Trying again" button.
  show({ kind: "signing-out" });
  await wipeBrowserState();
  publish({ state: "SIGNED_OUT", since: Date.now(), kind });
  navigate.replace(next);
}

/** Writes the record for the other tabs and posts it on the channel when there is one. */
function publish(record: Handoff): void {
  try {
    localStorage.setItem(HANDOFF_KEY, JSON.stringify(record));
  } catch {
    /* storage blocked: the channel still reaches the other tabs */
  }
  try {
    channel?.postMessage(record);
  } catch {
    /* the channel was closed under us */
  }
}

function applyRemote(record: Handoff | null): void {
  if (!record) return;
  if (record.state === "SIGNED_OUT") {
    wipeTabStorage();
    setOtherTab(causeOf(record.kind));
    return;
  }
  // A tab that already shows its result keeps it; only a tab still working mirrors a pending sign-out.
  if (screen.kind !== "other-tab") mirrorLeaving(record);
}

/** Shows this tab the pending sign-out another tab started, in the same screen (cover or failure). */
function mirrorLeaving(record: Extract<Handoff, { state: "LEAVING" }>): void {
  kind = record.kind;
  tries = record.tries;
  stop();
  if (record.failure) {
    show({ kind: "failed", reason: record.failure, tries: record.tries, trying: false });
    return;
  }
  show({ kind: "signing-out" });
  scheduleTakeover(record.since);
}

/** A mirrored sign-out whose starter closed before it published a result would wait forever. The starter
 * gives up within ANSWER_MS, so a record still unanswered after TAKEOVER_MS had no living starter: this
 * tab finishes the sign-out itself. */
const TAKEOVER_MS = ANSWER_MS + 5_000;

function scheduleTakeover(since: number): void {
  setTimeout(() => {
    const record = readHandoff();
    const stillWaiting =
      screen.kind === "signing-out" && attempt === null && record?.state === "LEAVING" && record.since === since;
    if (stillWaiting) void runSignOut();
  }, TAKEOVER_MS);
}

type Answer = { ended: true; next: string } | { ended: false; reason: FailureReason };

/** One attempt, under one budget of ANSWER_MS for everything it asks. */
async function gatewayAnswer(end: EndKind): Promise<Answer> {
  // No navigator.onLine check here: it is a hint, and a browser can be wrong about it. The request
  // itself decides; a dropped connection fails it as "no answer".
  const budget = new AbortController();
  const timer = setTimeout(() => budget.abort(), ANSWER_MS);
  try {
    return await askGateway(end, budget.signal);
  } catch (error) {
    return { ended: false, reason: error instanceof GatewayStatus ? "gateway-error" : "no-answer" };
  } finally {
    clearTimeout(timer);
  }
}

/** What this sign-out sends: the member's own CSRF token, or the owner's admin session's. "ended" when no
 * session is left to end; "refuse" when a session exists but no token can be had, so nothing is sent. */
type TokenAnswer = { kind: "token"; token: string } | { kind: "ended" } | { kind: "refuse" };

async function tokenFor(signal: AbortSignal): Promise<TokenAnswer> {
  const seen = await readSession(signal);
  if (seen.csrf) return { kind: "token", token: seen.csrf };
  const admin = await readAdmin(signal);
  if (admin.live) return admin.csrf ? { kind: "token", token: admin.csrf } : { kind: "refuse" };
  return seen.authenticated ? { kind: "refuse" } : { kind: "ended" };
}

async function askGateway(end: EndKind, signal: AbortSignal): Promise<Answer> {
  const path = end === "sign-out-everywhere" ? SIGN_OUT_EVERYWHERE_PATH : LOGOUT_PATH;
  let token = await tokenFor(signal);
  if (token.kind === "ended") return { ended: true, next: SIGN_IN_PATH };
  if (token.kind === "refuse") return { ended: false, reason: "gateway-error" };
  let response = await post(path, token.token, signal);
  if (response.status === 403) {
    // A stale CSRF token: one retry with a fresh one (DESIGN.md 4.4).
    token = await tokenFor(signal);
    if (token.kind === "ended") return { ended: true, next: SIGN_IN_PATH };
    if (token.kind === "refuse") return { ended: false, reason: "gateway-error" };
    response = await post(path, token.token, signal);
  }
  const body = await response.json().catch(() => null);
  if (response.ok) {
    // Only the gateway's own JSON answer ends the session. Any other 2xx (a captive portal, a proxy
    // page) is not an answer: the session decides.
    if (body !== null) return { ended: true, next: nextPage(body) };
    return (await sessionStillLive(signal))
      ? { ended: false, reason: "gateway-error" }
      : { ended: true, next: SIGN_IN_PATH };
  }
  if (response.status === 401 && errorCode(body) === "login_required") {
    return { ended: true, next: SIGN_IN_PATH };
  }
  if (response.status === 401 || response.status === 403) {
    // Still signed in means the gateway refused a live session: a gateway error, not an ended one.
    return (await sessionStillLive(signal))
      ? { ended: false, reason: "gateway-error" }
      : { ended: true, next: SIGN_IN_PATH };
  }
  return { ended: false, reason: "gateway-error" };
}

function post(path: string, token: string, signal: AbortSignal): Promise<Response> {
  return fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "x-azizam-csrf": token, accept: "application/json" },
    signal,
  });
}

async function sessionStillLive(signal: AbortSignal): Promise<boolean> {
  if ((await readSession(signal)).authenticated) return true;
  return (await readAdmin(signal)).live;
}

async function readSession(signal: AbortSignal): Promise<SessionView & { csrf: string | null }> {
  const response = await fetch(SESSION_PATH, { credentials: "same-origin", cache: "no-store", signal });
  if (response.status === 401) return { authenticated: false, epoch: null, person: null, csrf: null };
  if (!response.ok) throw new GatewayStatus(response.status);
  const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
  return {
    authenticated: body?.authenticated === true,
    epoch: typeof body?.session_view_id === "string" ? body.session_view_id : null,
    person: personOf(body),
    csrf: typeof body?.csrf_token === "string" && body.csrf_token ? body.csrf_token : null,
  };
}

/** The signed-in person's id from a `/me/api/session` snapshot, or null when there is none. */
export function personOf(body: Record<string, unknown> | null): string | null {
  const user = body?.user as { person_id?: unknown; id?: unknown } | undefined;
  const id = user?.person_id ?? user?.id;
  return typeof id === "string" && id !== "" ? id : null;
}

/** The owner's admin session, for a browser whose own snapshot has no CSRF token. Members get 403 here. */
async function readAdmin(signal: AbortSignal): Promise<{ live: boolean; csrf: string | null }> {
  const response = await fetch(ADMIN_SESSION_PATH, { credentials: "same-origin", cache: "no-store", signal });
  if (response.status === 401 || response.status === 403) return { live: false, csrf: null };
  if (!response.ok) throw new GatewayStatus(response.status);
  const body = (await response.json().catch(() => null)) as { actor?: { csrf_token?: unknown } } | null;
  const csrf = body?.actor?.csrf_token;
  return { live: true, csrf: typeof csrf === "string" && csrf ? csrf : null };
}

async function readSessionNow(): Promise<SessionView | null> {
  const budget = new AbortController();
  const timer = setTimeout(() => budget.abort(), ANSWER_MS);
  try {
    const { authenticated, epoch: seenEpoch, person } = await readSession(budget.signal);
    return { authenticated, epoch: seenEpoch, person };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

function nextPage(body: unknown): string {
  const next = (body as { next?: unknown } | null)?.next;
  // Only a path on this site; anything else goes to the sign-in page.
  return typeof next === "string" && /^\/(?!\/)/.test(next) ? next : SIGN_IN_PATH;
}

function errorCode(body: unknown): unknown {
  return (body as { error?: { code?: unknown } } | null)?.error?.code;
}

function parseHandoff(raw: unknown): Handoff | null {
  let value: unknown = raw;
  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (!END_KINDS.includes(record.kind as EndKind) || typeof record.since !== "number") return null;
  const end = record.kind as EndKind;
  if (record.state === "SIGNED_OUT") return { state: "SIGNED_OUT", since: record.since, kind: end };
  if (record.state !== "LEAVING") return null;
  const failure = record.failure ?? null;
  if (failure !== null && !FAILURES.includes(failure as FailureReason)) return null;
  return {
    state: "LEAVING",
    since: record.since,
    kind: end,
    failure: failure as FailureReason | null,
    tries: typeof record.tries === "number" ? record.tries : 0,
  };
}

function keepOnly(area: () => Storage, keep: string[]): void {
  try {
    const storage = area();
    for (const key of Object.keys(storage)) {
      if (!keep.includes(key)) storage.removeItem(key);
    }
  } catch {
    /* storage blocked: nothing to clear */
  }
}

async function dropDatabases(): Promise<void> {
  if (typeof indexedDB === "undefined" || typeof indexedDB.databases !== "function") return;
  for (const info of await indexedDB.databases()) {
    if (info.name) indexedDB.deleteDatabase(info.name);
  }
}

async function dropCaches(): Promise<void> {
  if (typeof caches === "undefined") return;
  for (const name of await caches.keys()) await caches.delete(name);
}

async function dropWorkers(): Promise<void> {
  const workers = typeof navigator === "undefined" ? undefined : navigator.serviceWorker;
  if (!workers) return;
  for (const registration of await workers.getRegistrations()) await registration.unregister();
}
