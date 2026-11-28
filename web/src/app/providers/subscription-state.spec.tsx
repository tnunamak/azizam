import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { SubscriptionDetails } from "./subscriptions-section";
import type { AccessPlan, App } from "@/lib/api";
import { disconnectImpact, impactSentence } from "./subscription-access";
import {
  formatResetTime,
  planUsageDetail,
  routeChainLines,
  sameRouteChain,
  subscriptionRowState,
  usageWindowName,
} from "./subscription-state";

const NOW = 1_800_000_000_000;
const sec = (agoSeconds: number) => NOW / 1000 - agoSeconds;
const chatgpt = { kind: "chatgpt", display_name: "ChatGPT" };

it("words each status the same way on every surface", () => {
  const notConnected = subscriptionRowState({ ...chatgpt, status: "not_connected" }, NOW);
  expect(notConnected).toMatchObject({ word: "Off", tone: "off", action: "connect" });
  expect(notConnected.detail).toBe("Let people and apps you choose use your ChatGPT models.");

  const connected = subscriptionRowState(
    { ...chatgpt, status: "connected", account_email: "owner@example.com", models: ["a", "b", "c"], models_checked_at: sec(240) },
    NOW,
  );
  expect(connected).toMatchObject({ word: "Ready", tone: "ok", action: "open" });
  expect(connected.detail).toBe("owner@example.com · 3 models · model list checked 4m ago");

  const down = subscriptionRowState({ ...chatgpt, status: "needs_reconnect", status_changed_at: sec(7200) }, NOW);
  expect(down).toMatchObject({ word: "Down", tone: "bad", action: "reconnect" });
  expect(down.detail).toContain("ChatGPT ended this sign-in 2h ago.");
  expect(down.detail).toContain("Requests to its models fail");
});

it("works with the health fields absent, as the gateway sends today", () => {
  const down = subscriptionRowState({ ...chatgpt, status: "needs_reconnect" }, NOW);
  expect(down.detail).toBe("ChatGPT ended this sign-in. Requests to its models fail until you connect again.");
  const ready = subscriptionRowState({ ...chatgpt, status: "connected", account_email: "o@example.com", models: ["m"] }, NOW);
  expect(ready.detail).toBe("o@example.com · 1 model");
});

it("says nothing about models until the list is known, and 0 models only for an empty list", () => {
  const unknown = subscriptionRowState({ ...chatgpt, status: "connected", account_email: "o@example.com", models_checked_at: sec(240) }, NOW);
  expect(unknown.detail).toBe("o@example.com");
  const empty = subscriptionRowState({ ...chatgpt, status: "connected", account_email: "o@example.com", models: [] }, NOW);
  expect(empty.detail).toBe("o@example.com · 0 models");
});

it("counts pinned names with the advertised list, once each, as the opened row does", () => {
  const pin = (slug: string) => ({ id: `sub/${slug}`, logical_model: slug, service_id: "sub", model_id: slug });
  const pinOnly = subscriptionRowState({ ...chatgpt, status: "connected", account_email: "o@example.com", models: [], routes: [pin("group-b")] }, NOW);
  expect(pinOnly.detail).toBe("o@example.com · 1 model");
  const beforeList = subscriptionRowState({ ...chatgpt, status: "connected", account_email: "o@example.com", routes: [pin("group-b")] }, NOW);
  expect(beforeList.detail).toBe("o@example.com · 1 model");
  const repeated = subscriptionRowState(
    { ...chatgpt, status: "connected", account_email: "o@example.com", models: ["gpt-z", "gpt-a", "gpt-a"], routes: [pin("gpt-a"), pin("group-b")] },
    NOW,
  );
  expect(repeated.detail).toBe("o@example.com · 3 models");
});

it("claims a shown list after a failed request only when there is a list", () => {
  const failed = { ...chatgpt, status: "connected", models_checked_at: sec(10_800), last_error: { code: "unreachable", at: sec(60) } };
  expect(subscriptionRowState(failed, NOW).detail).not.toContain("Showing the list");
  expect(subscriptionRowState({ ...failed, models: [] }, NOW).detail).toContain("Showing the list from");
});

it("says Degraded when the last check failed after the last good one", () => {
  const row = subscriptionRowState(
    { ...chatgpt, status: "connected", models: ["a"], models_checked_at: sec(10_800), last_error: { code: "unreachable", at: sec(60) } },
    NOW,
  );
  expect(row).toMatchObject({ word: "Degraded", tone: "warn", action: "check" });
  expect(row.detail).toContain("The last request to ChatGPT failed");
  expect(row.detail).toContain("Showing the list from");
});

it("names a usage limit in the last hour, and drops it once the hour is over", () => {
  const limited = { ...chatgpt, status: "connected", models: ["a"], last_error: { code: "plan_usage_limit_exceeded", at: sec(720) } };
  const row = subscriptionRowState(limited, NOW);
  expect(row).toMatchObject({ word: "Degraded", action: "open" });
  expect(row.detail).toMatch(/^ChatGPT said the usage limit was reached /);
  const old = subscriptionRowState({ ...limited, last_error: { code: "plan_usage_limit_exceeded", at: sec(7_200) } }, NOW);
  expect(old.detail).not.toContain("usage limit");
});

it("says who ended the sign-in when the gateway knows", () => {
  const revoked = subscriptionRowState({ ...chatgpt, status: "needs_reconnect", reconnect_reason: "revoked" }, NOW);
  expect(revoked.detail).toBe("This sign-in was ended. Requests to its models fail until you connect again.");
  expect(revoked.action).toBe("reconnect");
});

it("never offers Connect for a status it does not know", () => {
  const row = subscriptionRowState({ ...chatgpt, status: "connecting" }, NOW);
  expect(row).toMatchObject({ word: "Unknown", action: "check" });
  expect(row.detail).toBe("This page does not know this state. Check again, or open it for details.");
});

it("uses the kind's own name, never an id, for an unknown kind", () => {
  expect(subscriptionRowState({ kind: "other", display_name: "Other", status: "not_connected" }, NOW).detail).toContain("Other models");
});

const plan = (id: string, name: string, connections: string[]) =>
  ({ id, name, plan: { connections } }) as unknown as AccessPlan;
const app = (id: string, name: string, effective: string | null, disabled: number | null = null) =>
  ({ id, name, effective_access_plan_id: effective, disabled_at: disabled }) as unknown as App;

it("counts the access plans and live apps a disconnect reaches", () => {
  const impact = disconnectImpact(
    "chatgpt-owner",
    [plan("p1", "Household", ["chatgpt-owner"]), plan("p2", "Kids", []), plan("p3", "Local chat", ["chatgpt-owner", "x"])],
    [app("a1", "Notes", "p1"), app("a2", "Homework", "p2"), app("a3", "Chat", "p3"), app("a4", "Old", "p1", 5), app("a5", "None", null)],
  );
  expect(impact.plans.map((p) => p.name)).toEqual(["Household", "Local chat"]);
  expect(impact.apps.map((a) => a.name)).toEqual(["Notes", "Chat"]);
  expect(impactSentence(impact)).toBe("2 access plans (Household, Local chat) and 2 apps use it.");
  expect(impactSentence({ plans: [], apps: [] })).toBe("No access plan uses it.");
  expect(disconnectImpact(undefined, [plan("p1", "H", ["x"])], [])).toEqual({ plans: [], apps: [] });
});

// 2027-01-15 12:00:00 UTC, a Friday.
const NOON = Date.UTC(2027, 0, 15, 12, 0, 0);
const at = (secondsFromNoon: number) => NOON / 1000 + secondsFromNoon;
const usage = (capturedAgo: number) => ({
  captured_at: at(-capturedAgo),
  windows: [
    { used_percent: 42.4, window_minutes: 300, resets_at: at(3 * 3600 + 600) },
    { used_percent: 18, window_minutes: 10080, resets_at: at(6 * 86400) - 12 * 3600 },
  ],
});
const live = { ...chatgpt, status: "connected", account_email: "o@example.com", models: ["m"] };

it("names each window by its length", () => {
  expect(usageWindowName(90)).toBe("90-minute limit");
  expect(usageWindowName(120)).toBe("2-hour limit");
  expect(usageWindowName(300)).toBe("5-hour limit");
  expect(usageWindowName(2160)).toBe("36-hour limit");
  expect(usageWindowName(10080)).toBe("7-day limit");
  expect(usageWindowName(1440)).toBe("Daily limit");
  expect(usageWindowName(30)).toBe("30-minute limit");
  expect(usageWindowName(4320)).toBe("3-day limit");
  expect(usageWindowName(null)).toBe("Usage limit");
});

it("names a window that counts only some models, so two 7-day windows read apart", () => {
  const sub = {
    ...live,
    usage_limits: {
      captured_at: at(-60),
      windows: [
        { used_percent: 45, window_minutes: 10080, resets_at: at(86400) },
        { used_percent: 60, window_minutes: 10080, resets_at: at(86400), models: "Opus" },
        { used_percent: 10, window_minutes: null, resets_at: null, models: "Sonnet" },
      ],
    },
  };
  expect(subscriptionRowState(sub, NOON).detail).toBe(
    "o@example.com · 7-day limit 45% used · 7-day Opus limit 60% used · Sonnet usage limit 10% used · 1 model",
  );
});

it("says when a window resets: a time today, a weekday this week, else a date", () => {
  expect(formatResetTime(at(3 * 3600 + 600), NOON, "UTC")).toBe("3:10 PM");
  expect(formatResetTime(at(6 * 86400) - 12 * 3600, NOON, "UTC")).toBe("Thu 12:00 AM");
  expect(formatResetTime(at(20 * 86400), NOON, "UTC")).toBe("Feb 4");
});

it("shows a fresh reading as percent used and reset time", () => {
  const sub = { ...live, usage_limits: usage(120) };
  expect(subscriptionRowState(sub, NOON).detail).toBe("o@example.com · 5-hour limit 42% used · 7-day limit 18% used · 1 model");
  expect(planUsageDetail(sub, NOON, "UTC")).toEqual([
    "5-hour limit: 42% used, resets 3:10 PM",
    "7-day limit: 18% used, resets Thu 12:00 AM",
    "Read from ChatGPT 2m ago.",
  ]);
});

it("says out of date, with no number, once a reading is older than its window or its window reset", () => {
  const sub = { ...live, usage_limits: usage(6 * 3600) };
  expect(subscriptionRowState(sub, NOON).detail).toBe("o@example.com · 5-hour limit reading out of date · 7-day limit 18% used · 1 model");
  const lines = planUsageDetail(sub, NOON, "UTC");
  expect(lines[0]).toBe("5-hour limit: out of date (read 6h ago)");
  expect(lines[0]).not.toContain("42");
  const reset = { ...live, usage_limits: { captured_at: at(-60), windows: [{ used_percent: 90, window_minutes: 300, resets_at: at(-1) }] } };
  expect(planUsageDetail(reset, NOON, "UTC")[0]).toBe("5-hour limit: out of date (read 1m ago)");
});

it("never rounds a reading under the limit up to 100% used", () => {
  const reading = (percent: number) => ({
    ...live,
    usage_limits: { captured_at: at(-60), windows: [{ used_percent: percent, window_minutes: 300, resets_at: at(3600) }] },
  });
  expect(planUsageDetail(reading(99.6), NOON, "UTC")[0]).toBe("5-hour limit: 99% used, resets 1:00 PM");
  expect(planUsageDetail(reading(100), NOON, "UTC")[0]).toBe("5-hour limit: 100% used, resets 1:00 PM");
  expect(planUsageDetail(reading(42.6), NOON, "UTC")[0]).toBe("5-hour limit: 43% used, resets 1:00 PM");
});

it("keeps malformed percentages out of usage text and identifies overage without false precision", () => {
  for (const [percent, label] of [[105, "100% or more used"], [-3, "reading unavailable"], [NaN, "reading unavailable"]] as const) {
    const sub = { ...live, usage_limits: { captured_at: at(-60), windows: [{ used_percent: percent, window_minutes: 300, resets_at: null }] } };
    expect(subscriptionRowState(sub, NOON).detail).toBe(`o@example.com · 5-hour limit ${label} · 1 model`);
    expect(planUsageDetail(sub, NOON, "UTC")[0]).toBe(`5-hour limit: ${label}`);
  }
});

it("does not announce a reset from a non-finite full-window reading", () => {
  const sub = {
    ...live,
    usage_limits: { captured_at: at(-60), windows: [{ used_percent: Infinity, window_minutes: 300, resets_at: at(3600) }] },
    last_error: { code: "plan_usage_limit_exceeded", at: at(-60) },
  };
  expect(subscriptionRowState(sub, NOON).detail).toBe("ChatGPT said the usage limit was reached 1m ago.");
});

it("gives a fresh reading the unknown-window allowance for absent and nonpositive lengths", () => {
  for (const minutes of [-1, 0, null]) {
    const sub = { ...live, usage_limits: { captured_at: at(-60), windows: [{ used_percent: 42, window_minutes: minutes, resets_at: null }] } };
    expect(subscriptionRowState(sub, NOON).detail).toBe("o@example.com · usage limit 42% used · 1 model");
    expect(planUsageDetail(sub, NOON, "UTC")[0]).toBe("Usage limit: 42% used");
  }
});

it("treats a non-finite window length as unknown and ages it out", () => {
  const sub = {
    ...live,
    usage_limits: { captured_at: at(-60), windows: [{ used_percent: 42, window_minutes: Infinity, resets_at: null }] },
  };
  expect(subscriptionRowState(sub, NOON).detail).toBe("o@example.com · usage limit 42% used · 1 model");
  expect(planUsageDetail(sub, NOON, "UTC")[0]).toBe("Usage limit: 42% used");
  const later = NOON + 61 * 60 * 1000;
  expect(subscriptionRowState(sub, later).detail).toContain("usage limit reading out of date");
  expect(planUsageDetail(sub, later, "UTC")[0]).toContain("Usage limit: out of date");
});

it("omits an out-of-range reset time from a full limit row and its details", () => {
  const sub = {
    ...live,
    usage_limits: { captured_at: at(-60), windows: [{ used_percent: 100, window_minutes: 300, resets_at: 9e12 }] },
    last_error: { code: "plan_usage_limit_exceeded", at: at(-60) },
  };
  expect(subscriptionRowState(sub, NOON).detail).toBe("ChatGPT said the usage limit was reached 1m ago.");
  expect(planUsageDetail(sub, NOON, "UTC")[0]).toBe("5-hour limit: 100% used");
});

it("points to the provider's own usage page when the plan has sent no usage", () => {
  const withPage = { ...live, usage_page_url: "https://chatgpt.com/settings/usage" };
  expect(planUsageDetail(withPage, NOON)).toEqual(["ChatGPT has not sent plan usage to Azizam. ChatGPT shows it on its own Usage page."]);
  expect(planUsageDetail({ ...withPage, usage_limits: usage(120) }, NOON, "UTC")[0]).toBe("5-hour limit: 42% used, resets 3:10 PM");
});

it("says plainly when the plan has not reported usage yet", () => {
  expect(subscriptionRowState(live, NOON).detail).toBe("o@example.com · 1 model");
  expect(planUsageDetail(live, NOON)).toEqual(["Plan usage shows here only if ChatGPT reports it."]);
});

it("adds when a reached limit resets", () => {
  const full = { captured_at: at(-60), windows: [{ used_percent: 100, window_minutes: 300, resets_at: at(3 * 3600 + 600) }] };
  const row = subscriptionRowState({ ...live, usage_limits: full, last_error: { code: "plan_usage_limit_exceeded", at: at(-60) } }, NOON);
  expect(row.detail).toBe(`ChatGPT said the usage limit was reached 1m ago. 5-hour limit resets ${formatResetTime(full.windows[0].resets_at, NOON)}.`);
});

it("names the full window rather than the other usage window", () => {
  const reading = usage(60);
  const weeklyReset = reading.windows[1].resets_at;
  const row = subscriptionRowState({
    ...live,
    usage_limits: { ...reading, windows: [reading.windows[0], { ...reading.windows[1], used_percent: 100 }] },
    last_error: { code: "plan_usage_limit_exceeded", at: at(-60) },
  }, NOON);
  expect(row.detail).toBe(`ChatGPT said the usage limit was reached 1m ago. 7-day limit resets ${formatResetTime(weeklyReset, NOON)}.`);

});

it("adds reported token counts with coverage and sign-in scope", () => {
  const sub = { ...chatgpt, status: "connected", token_totals: {
    day: { requests: 1, input_tokens: 1200, cached_input_tokens: 800, output_tokens: 300 },
    week: { requests: 12, input_tokens: 45000, cached_input_tokens: 0, output_tokens: 2000 },
  } };
  const text = planUsageDetail(sub, NOW).join(" ");
  expect(text).toContain("1 request. Reported tokens: 1,200 input tokens");
  expect(text).toContain("800 cached input (subset)");
  expect(text).toContain("partial history");
  expect(text).toContain("including previous sign-ins");
});

const detailMarkup = (sub: Parameters<typeof SubscriptionDetails>[0]["sub"]) =>
  renderToStaticMarkup(<SubscriptionDetails sub={sub} subscriptions={[sub]} />);

it("renders an HTTPS usage link with a screen-reader cue and no link for invalid or non-HTTPS URLs", () => {
  const markup = detailMarkup({ ...live, usage_page_url: "https://example.test/usage" });
  expect(markup).toContain('href="https://example.test/usage" target="_blank"');
  expect(markup).toContain('Open ChatGPT usage<span class="sr-only"> (opens in a new tab)</span>');
  for (const usage_page_url of ["http://example.test/usage", "not a URL"]) {
    const rejected = detailMarkup({ ...live, usage_page_url });
    expect(rejected).not.toContain("Open ChatGPT usage");
    expect(rejected).toContain("Plan usage shows here only if ChatGPT reports it.");
  }
});

it("offers Check now in place when a connected subscription's models are unknown", () => {
  const markup = renderToStaticMarkup(
    <SubscriptionDetails sub={{ ...live, models: undefined }} subscriptions={[live]} onCheck={() => {}} />,
  );
  expect(markup).toContain("<p>No model list yet.</p>");
  expect(markup).toMatch(/<button[^>]*>Check now<\/button>/);
});

it("points at Check now only while the subscription is connected, the only time its menu offers it", () => {
  const markup = detailMarkup({ ...live, status: "needs_reconnect", models: undefined });
  expect(markup).toContain("<p>No model list yet.</p>");
  expect(markup).not.toContain("Check now");
});

it("words a route chain's steps in order, with the paid fallback condition and the failure at the end", () => {
  const subs = [
    { kind: "chatgpt", display_name: "ChatGPT", status: "connected", connection_id: "c-1" },
    { kind: "claude", display_name: "Claude", status: "connected", connection_id: "c-2" },
  ];
  const plan = (connection_id: string) => ({ route_id: `p/${connection_id}`, kind: "plan" as const, service_id: "p", name: "p", paid_fallback: false, connection_id });
  const paid = { route_id: "k", kind: "paid" as const, service_id: "openai", name: "OpenAI", paid_fallback: true, provider_id: "openai" };
  const local = { route_id: "l", kind: "local" as const, service_id: "box", name: "Box", paid_fallback: false };
  const chain = { slug: "m", direct: false, steps: [plan("c-1"), plan("c-2"), plan("gone"), paid, local] };
  expect(detailMarkup({ ...live, models: ["m"], model_chains: [chain] })).toContain(
    "If a paid key ends up first for someone, it is used directly, not as a fallback.",
  );
  expect(routeChainLines(chain, subs)).toEqual([
    "1. Your ChatGPT plan.",
    "2. Your Claude plan.",
    "3. Another connected plan.",
    "4. Paid OpenAI key, only when the person's and the app's access plans both allow paid fallback.",
    "5. Local engine Box.",
    "6. Otherwise the request fails with a message.",
  ]);
  expect(routeChainLines({ slug: "m", direct: false, steps: [{ ...paid, paid_fallback: false }] }, subs)).toEqual([
    "1. Paid OpenAI key.",
    "2. Otherwise the request fails with a message.",
  ]);
  expect(routeChainLines({ slug: "m", direct: false, steps: [] }, subs)).toEqual([
    "A plain m request does not reach this plan. Use the pinned name.",
  ]);
  expect(sameRouteChain({ ...chain, slug: "n", steps: chain.steps.map((step) => ({ ...step, route_id: "other" })) }, chain)).toBe(true);
  expect(sameRouteChain({ ...chain, steps: [plan("c-1")] }, chain)).toBe(false);
});

it("trims model labels and keeps literal dollar text in usage window names", () => {
  const detail = (models: string, window_minutes: number | null) => {
    const sub = { ...live, usage_limits: { captured_at: at(-60), windows: [
      { used_percent: 25, window_minutes, resets_at: null, models },
    ] } };
    return { full: planUsageDetail(sub, NOON, "UTC")[0], short: subscriptionRowState(sub, NOON).detail };
  };
  expect(detail("Opus ", 1440).full).toBe("Daily Opus limit: 25% used");
  expect(detail("  ", 1440).full).toBe("Daily limit: 25% used");
  expect(detail(" Sonnet ", null).full).toBe("Sonnet usage limit: 25% used");
  expect(detail(" Sonnet ", null).short).toContain("· Sonnet usage limit 25% used ·");
  expect(detail("Opus $&", 1440).full).toBe("Daily Opus $& limit: 25% used");
});

it("names a full model window in the reached sentence, but keeps the generic fallback", () => {
  const resets_at = at(86400);
  const detail = (models?: string) => subscriptionRowState({
    ...live,
    usage_limits: { captured_at: at(-60), windows: [
      { used_percent: 100, window_minutes: 10080, resets_at, ...(models === undefined ? {} : { models }) },
    ] },
    last_error: { code: "plan_usage_limit_exceeded", at: at(-60) },
  }, NOON).detail;
  expect(detail(" Opus ")).toBe(`ChatGPT said the 7-day Opus limit was reached 1m ago. 7-day Opus limit resets ${formatResetTime(resets_at, NOON)}.`);
  expect(detail()).toBe(`ChatGPT said the usage limit was reached 1m ago. 7-day limit resets ${formatResetTime(resets_at, NOON)}.`);
});

it("labels partial recorded counts and distinct allowance scopes", () => {
  const sub = { ...chatgpt, status: "connected", usage_limits: { captured_at: sec(1), windows: [
    { id: "base", scope: "Account", used_percent: 10, window_minutes: 10080, resets_at: sec(-60) },
    { id: "extra", scope: "Overage", used_percent: 20, window_minutes: 10080, resets_at: sec(-60) },
  ] }, token_totals: { read_at: sec(2), day: { requests: 1, input_tokens: 5, cached_input_tokens: 0,
    output_tokens: 2, incomplete_attempts: 1, covered_since: sec(30) },
    week: { requests: 1, input_tokens: 5, cached_input_tokens: 0, output_tokens: 2, incomplete_attempts: 1, covered_since: sec(30) } } };
  const text = planUsageDetail(sub, NOW).join(" ");
  expect(text).toContain("Account");
  expect(text).toContain("Overage");
  expect(text).toContain("Reported tokens");
  expect(text).toContain("1 attempt without complete usage");
  expect(text).toContain("partial history");
  expect(text).toContain("Counts as of");
});
