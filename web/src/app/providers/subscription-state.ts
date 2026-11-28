import type { ConnectionStatus, PlanModelChain, PlanTokenTotals, PlanUsageWindow, RouteChainStep } from "@/lib/api";
import { STATE_WORDS, type CapabilityStateKey, type StateTone } from "@/lib/capability-state";
import { formatNumber, formatRelativeTime } from "@/lib/format";

export type SubscriptionAction = "connect" | "reconnect" | "open" | "check";

export interface SubscriptionRowState {
  state: CapabilityStateKey;
  word: string;
  tone: StateTone;
  detail: string;
  action: SubscriptionAction;
}

/** The owner's noun for one kind, from the kind's own name. */
export function subscriptionLabel(sub: Pick<ConnectionStatus, "kind" | "display_name">): string {
  return `${sub.display_name || sub.kind} subscription`;
}

/**
 * The models a subscription offers, as both the row and its opened detail count them: the advertised list plus pinned
 * names, unique and sorted. Undefined while the gateway has read no list and nothing is pinned: no list yet is not an
 * empty list.
 */
export function subscriptionModelNames(sub: Pick<ConnectionStatus, "models" | "routes">): string[] | undefined {
  const pinned = (sub.routes ?? []).map((route) => route.logical_model);
  if (sub.models == null && pinned.length === 0) return undefined;
  return [...new Set([...(sub.models ?? []), ...pinned])].sort((a, b) => a.localeCompare(b));
}

/**
 * One row's state word, its one detail line and its one action: a function of
 * `status` and the optional health fields, so every surface says the same.
 */
export function subscriptionRowState(sub: ConnectionStatus, nowMs: number = Date.now()): SubscriptionRowState {
  const name = sub.display_name || sub.kind;
  const make = (state: CapabilityStateKey, detail: string, action: SubscriptionAction): SubscriptionRowState => ({
    state,
    word: STATE_WORDS[state].word,
    tone: STATE_WORDS[state].tone,
    detail,
    action,
  });
  if (sub.status === "needs_reconnect") {
    const since = sub.status_changed_at ? ` ${formatRelativeTime(sub.status_changed_at, nowMs)}` : "";
    const why = sub.reconnect_reason === "revoked" ? `This sign-in was ended${since}` : `${name} ended this sign-in${since}`;
    return make("down", `${why}. Requests to its models fail until you connect again.`, "reconnect");
  }
  if (sub.status === "connected") {
    // No list yet is not an empty list: say nothing about models until the gateway has read them.
    const models = subscriptionModelNames(sub)?.length;
    const parts = [
      sub.account_email,
      ...planUsage(sub, nowMs).map((line) => line.short),
      models === undefined ? null : `${models} ${models === 1 ? "model" : "models"}`,
      models !== undefined && sub.models_checked_at ? `model list checked ${formatRelativeTime(sub.models_checked_at, nowMs)}` : null,
    ].filter(Boolean);
    // The gateway clears last_error on the next success, so a present error is the current one.
    const error = sub.last_error;
    if (error && error.code.includes("usage_limit") && nowMs - error.at * 1000 < 60 * 60 * 1000) {
      const full = planUsage(sub, nowMs).find((line) => !line.stale && Number.isFinite(line.window.used_percent) && line.window.used_percent >= 100 && validResetDate(line.window.resets_at) !== undefined);
      const resetTime = full ? formatResetTime(full.window.resets_at, nowMs) : "";
      const resets = full && resetTime ? ` ${windowName(full.window)} resets ${resetTime}.` : "";
      const reached = full?.window.models?.trim() ? windowName(full.window) : "usage limit";
      return make("degraded", `${name} said the ${reached} was reached ${formatRelativeTime(error.at, nowMs)}.${resets}`, "open");
    }
    if (error) {
      // Claim a shown list only when there is one to show.
      const shown = sub.models != null && sub.models_checked_at
        ? ` Showing the list from ${formatRelativeTime(sub.models_checked_at, nowMs)}.`
        : "";
      return make("degraded", `The last request to ${name} failed ${formatRelativeTime(error.at, nowMs)}.${shown}`, "check");
    }
    return make("ready", parts.join(" · "), "open");
  }
  if (sub.status === "not_connected" || sub.status === "disconnected") {
    return make("off", sub.status === "disconnected" ? "Disconnected. Connect again to use its models." : `Let people and apps you choose use your ${name} models.`, "connect");
  }
  // A status this page does not know: never offer Connect, which could replace a live sign-in.
  return make("unknown", "This page does not know this state. Check again, or open it for details.", "check");
}

/** A reading of a window whose length the plan did not say is out of date after this many minutes. */
const UNKNOWN_WINDOW_MINUTES = 60;

function knownWindowMinutes(minutes: number | null): minutes is number {
  return typeof minutes === "number" && Number.isFinite(minutes) && minutes > 0;
}

/** A window's plain name uses its exact length, never a rounded hour or day. */
export function usageWindowName(minutes: number | null): string {
  if (!knownWindowMinutes(minutes)) return "Usage limit";
  if (minutes === 1440) return "Daily limit";
  if (minutes % 1440 === 0) return `${minutes / 1440}-day limit`;
  if (minutes % 60 === 0) return `${minutes / 60}-hour limit`;
  return `${minutes}-minute limit`;
}

/** A window's name, with the models it counts when it counts only some: "7-day Opus limit". */
function windowName(window: PlanUsageWindow, models = window.models?.trim() ?? ""): string {
  const name = usageWindowName(window.window_minutes);
  if (!models) return name;
  return name === "Usage limit" ? `${models} usage limit` : name.replace(/ limit$/, () => ` ${models} limit`);
}

function validResetDate(seconds: number | null): Date | undefined {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return undefined;
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

/** When a window resets, in the reader's time: "3:10 PM" today, "Thu 3:10 PM" this week, else "Nov 30". */
export function formatResetTime(resetsAt: number | null, nowMs: number, timeZone?: string): string {
  const at = validResetDate(resetsAt);
  if (!at) return "";
  const day = (date: Date) => date.toLocaleDateString("en-CA", { timeZone });
  const time = at.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone });
  if (day(at) === day(new Date(nowMs))) return time;
  if (at.getTime() - nowMs < 6 * 24 * 60 * 60 * 1000) {
    return `${at.toLocaleDateString("en-US", { weekday: "short", timeZone })} ${time}`;
  }
  return at.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone });
}

export interface PlanUsageLine {
  window: PlanUsageWindow;
  /** True when the reading can no longer be right: its window reset, or it is older than the window. */
  stale: boolean;
  /** For the detail: "5-hour limit: 42% used, resets 3:10 PM". */
  text: string;
  /** For the row: "5-hour limit 42% used". */
  short: string;
}

/**
 * The plan's usage windows as the owner reads them. A reading older than its
 * window, or one whose window has reset, says "out of date" and shows no
 * number: the number it had is no longer true.
 */
export function planUsage(sub: ConnectionStatus, nowMs: number = Date.now(), timeZone?: string): PlanUsageLine[] {
  const usage = sub.usage_limits;
  if (!usage) return [];
  const ageMs = nowMs - usage.captured_at * 1000;
  return usage.windows.map((window) => {
    const models = window.models?.trim() ?? "";
    const name = windowName(window, models);
    // A model name keeps its capital: "Sonnet usage limit reading out of date".
    const lower = models && name.startsWith(models) ? name : name.charAt(0).toLowerCase() + name.slice(1);
    const resetAt = validResetDate(window.resets_at);
    const reset = resetAt !== undefined && nowMs >= resetAt.getTime();
    const minutes = knownWindowMinutes(window.window_minutes) ? window.window_minutes : UNKNOWN_WINDOW_MINUTES;
    const stale = reset || ageMs > minutes * 60 * 1000;
    if (stale) {
      return { window, stale, text: `${name}: out of date (read ${formatRelativeTime(usage.captured_at, nowMs)})`, short: `${lower} reading out of date` };
    }
    // Never round a reading under the limit up to 100%: the limit is not reached yet.
    const percent = window.used_percent < 100 ? Math.min(Math.round(window.used_percent), 99) : Math.round(window.used_percent);
    const used = !Number.isFinite(window.used_percent) || window.used_percent < 0
      ? "reading unavailable"
      : window.used_percent > 100 ? "100% or more used" : `${percent}% used`;
    const resetTime = formatResetTime(window.resets_at, nowMs, timeZone);
    const resets = resetTime ? `, resets ${resetTime}` : "";
    return { window, stale, text: `${name}: ${used}${resets}`, short: `${lower} ${used}` };
  });
}

/** The requests and tokens Azizam counted on this plan: one line per span, or none when the gateway sent none. */
export function planTokenLines(sub: ConnectionStatus): string[] {
  const totals = sub.token_totals;
  if (!totals) return [];
  const line = (span: string, t: PlanTokenTotals) => {
    const cached = t.cached_input_tokens ? ` (${formatNumber(t.cached_input_tokens)} cached)` : "";
    return `${span} through Azizam: ${formatNumber(t.requests)} ${t.requests === 1 ? "request" : "requests"}, ` +
      `${formatNumber(t.input_tokens)} input tokens${cached}, ${formatNumber(t.output_tokens)} output tokens.`;
  };
  return [line("Last 24 hours", totals.day), line("Last 7 days", totals.week)];
}

/** The provider's usage link, when it points to an HTTPS URL. */
export function planUsagePageUrl(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

/** The detail's usage lines: each window, when it was read, and Azizam's own counts. */
export function planUsageDetail(sub: ConnectionStatus, nowMs: number = Date.now(), timeZone?: string): string[] {
  const name = sub.display_name || sub.kind;
  const lines = planUsage(sub, nowMs, timeZone);
  const usage = sub.usage_limits && lines.length
    ? [...lines.map((line) => line.text), `Read from ${name} ${formatRelativeTime(sub.usage_limits.captured_at, nowMs)}.`]
    : planUsagePageUrl(sub.usage_page_url)
      ? [`${name} has not sent plan usage to Azizam. ${name} shows it on its own Usage page.`]
      : [`Plan usage shows here only if ${name} reports it.`];
  const tokens = planTokenLines(sub);
  return tokens.length ? [...usage, "Azizam's own counts for this plan:", ...tokens] : usage;
}

/** The owner's words for one step: which plan, paid key or engine serves it. */
export function routeStepName(step: RouteChainStep, subscriptions: ConnectionStatus[]): string {
  if (step.kind === "plan") {
    const sub = subscriptions.find((row) => row.connection_id && row.connection_id === step.connection_id);
    return sub ? `Your ${sub.display_name || sub.kind} plan` : "Another connected plan";
  }
  return step.kind === "paid" ? `Paid ${step.name} key` : `Local engine ${step.name}`;
}

/**
 * The numbered steps a plain-name request walks, in plain words, ending with what happens when no step answers.
 * A paid step after another step names its condition: only when the person's and the app's access plans both allow paid fallback.
 */
export function routeChainLines(chain: PlanModelChain, subscriptions: ConnectionStatus[]): string[] {
  if (chain.direct) {
    const to = chain.steps[0] ? ` It goes to: ${routeStepName(chain.steps[0], subscriptions)}.` : "";
    return [`A plain ${chain.slug} request does not use this plan.${to}`];
  }
  if (chain.steps.length === 0) return [`A plain ${chain.slug} request does not reach this plan. Use the pinned name.`];
  const lines = chain.steps.map((step, index) => {
    const condition = step.paid_fallback ? ", only when the person's and the app's access plans both allow paid fallback" : "";
    return `${index + 1}. ${routeStepName(step, subscriptions)}${condition}.`;
  });
  return [...lines, `${lines.length + 1}. Otherwise the request fails with a message.`];
}

/** Two chains read the same when their steps name the same servers under the same conditions; route ids differ per model. */
export function sameRouteChain(a: PlanModelChain, b: PlanModelChain): boolean {
  const words = (chain: PlanModelChain) =>
    JSON.stringify([chain.direct, chain.steps.map((s) => [s.kind, s.connection_id, s.provider_id, s.name, s.paid_fallback])]);
  return words(a) === words(b);
}
