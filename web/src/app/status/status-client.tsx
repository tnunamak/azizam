"use client";

import Link from "next/link";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, MoreHorizontal, RefreshCcw, Server } from "lucide-react";

import {
  api,
  ApiError,
  queryKeys,
  type CurrentHealthService,
  type Profile,
  type Service,
  type ServiceRegistry,
  type StatusPayload,
} from "@/lib/api";
import { PRODUCT_NAME } from "@/lib/product";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  useOptionalProviderStatus,
  type ProviderStatusView,
} from "@/lib/provider-status";
import {
  engineDisplayName,
  engineHealthPresentation,
  engineShortName,
} from "@/lib/health-presentation";
import { cn } from "@/lib/cn";
import type { PlanSpend } from "@/lib/product-api-types";
import { SectionHeading } from "@/components/product/readiness";
import {
  Callout,
  CapabilityChips,
  CapabilityList,
  StateWord,
  StatusLine,
  type CalloutSeverity,
} from "@/components/product/status-ui";
import {
  healthHistory,
  healthStory,
  hourSlots,
  hoursUp,
  UptimeBar,
} from "@/components/product/uptime-bar";
import { PageHeader } from "@/components/product/page-header";
import { KeyValueList } from "@/components/product/key-value";
import { PersonMark } from "@/components/product/person-mark";
import {
  isInFlight,
  isSuccess,
  ageText,
  CAP_WARNING,
  capRefusalCurrent,
  FRESH_S,
  engineRemedy,
  engineSymptom,
  isTerminalFailure,
  requestCapability,
  STATE_WORDS,
  type CapabilityStateKey,
  type CapabilityView,
  type StateTone,
} from "@/lib/capability-state";
import {
  capabilityLabel,
  useActivitySample,
  useCapabilityViews,
  type ActivityRecord,
} from "@/lib/use-capability-views";

type LiveBackend = NonNullable<StatusPayload["backends"]>[number];
type EngineView = {
  service: Service;
  health?: CurrentHealthService;
  backend?: LiveBackend;
  name: string;
  shortName: string;
  label: string;
  role: string;
  state: string;
  tone: "ok" | "warn" | "bad" | "neutral";
  endpoint: string;
  models: string[];
  queue: string;
  lastError: string | null;
  checkedAt: number | null;
};

function titleize(value: string): string {
  return value
    .replace(/[._-]/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function valueText(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "number")
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function endpoint(service: Service, backend?: LiveBackend): string {
  const raw =
    service.config.base_url ||
    backend?.url ||
    (service.config.host
      ? `${service.config.host}${service.config.port ? `:${service.config.port}` : ""}`
      : "");
  if (!raw) return "Not configured";
  try {
    return new URL(String(raw)).host;
  } catch {
    return String(raw);
  }
}
function serviceModels(service: Service): string[] {
  const models = service.config.models;
  if (Array.isArray(models)) {
    return models
      .map((model) => model.model_group || model.upstream_model)
      .filter((model): model is string => Boolean(model));
  }
  if (models && typeof models === "object") {
    return Object.keys(models as Record<string, unknown>).filter(Boolean);
  }
  return [];
}

function enginesFrom(
  registry: ServiceRegistry | undefined,
  status: StatusPayload | undefined,
  presentationStatus: StatusPayload | undefined,
): EngineView[] {
  const presentation = new Map(
    (presentationStatus?.current_health?.services ?? []).map((item) => [
      item.service_id,
      item,
    ]),
  );
  const health = new Map(
    (status?.current_health?.services ?? []).map((item) => [
      item.service_id,
      item,
    ]),
  );
  const backends = new Map(
    (presentationStatus?.backends ?? []).flatMap((backend) => {
      const id = backend.service_id || backend.key;
      return id ? [[id, backend] as const] : [];
    }),
  );
  return Object.values(registry?.services ?? {})
    .map((service) => {
      const h = health.get(service.id);
      const display = presentation.get(service.id);
      const backend = backends.get(service.id);
      const backendExtras = backend as
        | (LiveBackend & { queue?: unknown; queue_depth?: unknown })
        | undefined;
      const healthPresentation = engineHealthPresentation(
        service.enabled,
        status?.current_health?.observation_available === true,
        h,
        status ? (backend?.live_status ?? backend?.status) : undefined,
      );
      return {
        service,
        health: h,
        backend,
        name: engineDisplayName(service, display),
        shortName: engineShortName(service, display),
        // People's names for capabilities (VOICE glossary): Speech is Voice.
        label:
          display?.capability_labels
            ?.filter((label) => !/\((?:speech|transcription)\)/i.test(label))
            .map((label) => (label === "Speech" ? "Voice" : label))
            .join(", ") || "",
        role: service.role || h?.role || titleize(service.kind || "engine"),
        state: healthPresentation.label,
        tone: healthPresentation.tone,
        endpoint: endpoint(service, backend),
        models: serviceModels(service),
        queue: valueText(backendExtras?.queue ?? backendExtras?.queue_depth),
        lastError: h?.last_error ?? backend?.last_error ?? null,
        // The poller's record time, never the payload's generation time.
        checkedAt:
          (backend as { observed_at_s?: number | null } | undefined)
            ?.observed_at_s ?? null,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function EngineSheet({
  engine,
  open,
  onOpenChange,
  children,
}: {
  engine: EngineView;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The control that opens the sheet; focus returns to it on close. */
  children: React.ReactNode;
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetTrigger asChild>{children}</SheetTrigger>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{engine.name}</SheetTitle>
          <SheetDescription>
            {[engine.label, engine.state].filter(Boolean).join(" · ")}
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-5 px-4 pb-6 font-normal">
          <dl className="space-y-3 text-meta">
            {[
              ["Role", engine.role],
              ...(engine.endpoint ? [["Endpoint", engine.endpoint]] : []),
              ...(engine.queue ? [["Queue", engine.queue]] : []),
              ...(engine.models.length
                ? [["Served models", engine.models.join(", ")]]
                : []),
              ...(engine.lastError ? [["Last error", engine.lastError]] : []),
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-all">{value}</dd>
              </div>
            ))}
          </dl>
          <Button variant="outline" asChild>
            <Link
              href={`/engines?service=${encodeURIComponent(engine.service.id)}`}
            >
              Configure engine
            </Link>
          </Button>
          <details>
            <summary className="min-h-8 max-sm:min-h-11 cursor-pointer py-3 text-meta">
              Technical fields
            </summary>
            <pre className="bg-muted rounded-control max-h-96 overflow-auto p-3 text-meta">
              {JSON.stringify(engine.service, null, 2)}
            </pre>
          </details>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function joinNames(names: string[]): string {
  if (names.length < 3) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** Engine rows speak the same state words as capabilities. */
function engineStateKey(engine: EngineView): CapabilityStateKey {
  if (!engine.service.enabled) return "off";
  if (["Ready", "Ready to start", "Standby, stopped"].includes(engine.state))
    return "ready";
  if (engine.state === "Busy" || engine.state === "Starting") return "busy";
  if (engine.state === "Degraded") return "degraded";
  if (engine.state === "Unavailable") return "down";
  if (engine.state === "Disabled") return "off";
  return "unknown";
}

function stateWordFor(key: CapabilityStateKey) {
  return { word: STATE_WORDS[key].word, tone: STATE_WORDS[key].tone };
}

/**
 * Failed requests as evidence: how many of the sample, from whom, when, and
 * the latest receipt. `who: false` leaves out the people and the time when
 * the card's "Affects" already names them.
 */
function failedRequestsFact(
  records: ActivityRecord[],
  sampleSize: number,
  { who = true }: { who?: boolean } = {},
): ReactNode {
  if (!records.length) return null;
  const people = who
    ? ([
        ...new Set(
          records.map((record) => record.person_label).filter(Boolean),
        ),
      ] as string[])
    : [];
  const latest = records[0];
  return (
    <span className="flex items-start gap-2">
      {people.length ? (
        <span className="flex shrink-0 -space-x-1 pt-px">
          {people.slice(0, 4).map((name) => (
            <PersonMark
              key={name}
              name={name}
              size="xs"
              label={name}
              className="ring-card ring-2"
            />
          ))}
        </span>
      ) : null}
      <span className="min-w-0">
        {records.length} of the latest {sampleSize} requests
        {people.length ? `, from ${joinNames(people)}` : ""}
        {who ? (
          <>
            ; latest <RelativeTime ts={latest.created_at ?? null} fallback="" />
          </>
        ) : null}
        {latest.receipt?.request_id ? (
          <>
            {/* On a phone the link takes its own line, so no dot dangles. */}
            <span aria-hidden="true" className="max-sm:hidden">
              {" · "}
            </span>
            <a
              href={`/r/${encodeURIComponent(latest.receipt.request_id)}`}
              className="text-link font-medium underline-offset-4 hover:underline max-sm:block max-sm:w-fit max-sm:py-3"
            >
              Open receipt
            </a>
          </>
        ) : null}
      </span>
    </span>
  );
}

type Problem = {
  key: string;
  severity: CalloutSeverity;
  tone: StateTone;
  /** Names the subject and its state word: "Video studio is down". */
  title: string;
  facts: Array<[string, ReactNode]>;
  /** Supporting evidence, folded behind "Show details". */
  evidence?: Array<[string, ReactNode]>;
  /** The 24-hour strip and its story, after what to do (D1.6). */
  history?: ReactNode;
  /** Plain paragraphs: the symptom, then the fix. */
  body?: string[];
  href?: string | null;
  action?: string;
  disk?: boolean;
  retry?: () => void;
  /** Short verdict when this is the only problem. */
  headline: string;
};

function backendFailures(records: ActivityRecord[], ids: Set<string>) {
  // Only finished failures count; queued or running work is not a failure.
  return records.filter(
    (record) =>
      record.receipt?.backend &&
      ids.has(record.receipt.backend) &&
      isTerminalFailure(record.receipt.status),
  );
}

/**
 * Who and what a failing engine affects: the capabilities left without
 * another engine, and the people (and their apps) whose requests failed.
 */
function affectsFact(
  exposed: string[],
  covered: string[],
  failed: ActivityRecord[],
): ReactNode {
  // More than three names would repeat the Capabilities list on the same
  // screen; count them instead.
  const scope = exposed.length
    ? exposed.length > 3
      ? `${exposed.length} capabilities: no other engine serves them.`
      : `${joinNames(exposed)}: no other engine serves ${exposed.length === 1 ? "it" : "them"}.`
    : covered.length
      ? `Nothing yet: another engine still serves ${joinNames(covered.map((label) => label.toLowerCase()))}.`
      : "Unknown";
  const people = [
    ...new Set(failed.map((record) => record.person_label).filter(Boolean)),
  ] as string[];
  const latest = failed[0];
  let who: ReactNode = null;
  if (people.length === 1 && failed.length === 1) {
    const app = latest.app_label;
    // An app often carries its owner's name ("Robin's personal app").
    const namedApp = app?.toLowerCase().includes(people[0].toLowerCase());
    who = (
      <>
        {namedApp ? `A request from ${app}` : `${people[0]}’s request`}
        {app && !namedApp ? ` from ${app}` : ""} failed{" "}
        <RelativeTime ts={latest.created_at ?? null} fallback="" />.
      </>
    );
  } else if (people.length)
    who = (
      <>
        {failed.length} requests from {joinNames(people)} failed; latest{" "}
        <RelativeTime ts={latest.created_at ?? null} fallback="" />.
      </>
    );
  return (
    <span className="block space-y-1">
      <span className="block">{scope}</span>
      {who ? (
        // On a phone the scope line stands alone; the receipt is in the
        // folded evidence (D1.4: one line of scope).
        <span className="flex items-start gap-2 max-sm:hidden">
          <span className="flex shrink-0 -space-x-1 pt-px">
            {people.slice(0, 4).map((name) => (
              <PersonMark
                key={name}
                name={name}
                size="xs"
                className="ring-card ring-2"
              />
            ))}
          </span>
          <span className="min-w-0">{who}</span>
        </span>
      ) : null}
    </span>
  );
}

/**
 * Capabilities that read Unknown with no card to explain them: the gateway
 * does not name an engine for them that is failing or unchecked (red team
 * round 2: Voice and Transcription were Unknown with no card). One card
 * names them, says what is known, and points at Engines.
 */
function unexplainedProblems(
  views: CapabilityView[],
  troubledEngines: EngineView[],
): Problem[] {
  const explained = new Set(
    troubledEngines.flatMap((engine) =>
      engine.label ? engine.label.split(", ") : [],
    ),
  );
  // Only a check that went stale or got no answer; a capability the gateway
  // never checks stays a quiet Unknown row, as engines do.
  const unknown = views.filter(
    (view) =>
      view.state === "unknown" && view.detail && !explained.has(view.label),
  );
  if (!unknown.length) return [];
  const names = joinNames(unknown.map((view) => view.label));
  return [
    {
      key: `unconfirmed:${unknown.map((view) => view.key).join(",")}`,
      severity: "attention",
      tone: "unknown",
      title: `${names} ${unknown.length === 1 ? "is" : "are"} not confirmed`,
      headline: `${names} unknown`,
      facts: [],
      body: [
        unknown[0].detail ?? "No recent check has answered for them.",
        "The gateway does not say which engine serves them.",
      ],
      href: "/engines/",
      action: "Open Engines",
    },
  ];
}

function symptomOf(engine: EngineView): string {
  return engineSymptom({
    name: engine.shortName,
    lastError: engine.lastError,
    observation: engine.state === "Degraded" ? "degraded" : "down",
  });
}

/** Failing engines, grouped when they fail the same way. */
function engineProblems(
  engines: EngineView[],
  records: ActivityRecord[],
  end: number,
  activityFallback: ReactNode,
): Problem[] {
  const grouped = new Map<string, EngineView[]>();
  for (const engine of engines) {
    const key = engineStateKey(engine);
    if (!["down", "degraded"].includes(key)) continue;
    // A stopped standby engine nobody uses is not a problem: the gateway
    // marks it neutral.
    if (engine.health?.tone === "neutral" && engine.health.in_use === false)
      continue;
    const symptom = symptomOf(engine).replace(engine.shortName, "");
    grouped.set(symptom, [...(grouped.get(symptom) ?? []), engine]);
  }
  return [...grouped.values()].map((affected) => {
    const first = affected[0];
    const key = affected.every(
      (engine) => engineStateKey(engine) === "degraded",
    )
      ? "degraded"
      : "down";
    const word = STATE_WORDS[key].word.toLowerCase();
    const labels = [
      ...new Set(
        affected.flatMap((engine) =>
          engine.label ? engine.label.split(", ") : [],
        ),
      ),
    ];
    const failed = backendFailures(
      records,
      new Set(affected.map((engine) => engine.service.id)),
    );
    // A capability with another ready engine still works: say so, and keep
    // it out of the verdict.
    const covered = labels.filter((label) =>
      engines.some(
        (engine) =>
          !affected.includes(engine) &&
          engineStateKey(engine) === "ready" &&
          (engine.label ? engine.label.split(", ") : []).includes(label),
      ),
    );
    const exposed = labels.filter((label) => !covered.includes(label));
    // Scope first: what stops working, for whom, and who already hit it
    // (not the capability name again; the title names the engine).
    const single = affected.length === 1;
    // D1.6: one title on the page. The verdict above names the state
    // ("Video degraded"); a single engine's card names the consequence
    // ("Video requests can fail right now") and its first fact the cause.
    const consequence = single && exposed.length > 0;
    const facts: Array<[string, ReactNode]> = [
      ...(consequence
        ? ([["What's wrong", symptomOf(first)]] as Array<[string, ReactNode]>)
        : []),
      ["Affects", affectsFact(exposed, covered, failed)],
    ];
    const evidence: Array<[string, ReactNode]> = [];
    let history: ReactNode = null;
    if (single) {
      const series = first.backend?.history?.status_series;
      const slots = hourSlots(series, end);
      // The words come from the same hourly slots the strip draws, so the
      // card never calls a day of failures a short, recent problem.
      const story = healthStory(healthHistory(slots), slots);
      // Where it stands, the day's spells, how the red hours relate to the
      // verdict, then the strip: history comes after what to do (D1.6).
      const { down } = healthHistory(slots);
      const relation =
        story.current?.startsWith("Degraded") && down > 0
          ? "Fully down at times (red); partly working now (amber)."
          : null;
      if (story.current || series?.length)
        history = (
          <div className="space-y-2">
            <h4 className="text-muted-foreground text-meta font-medium">
              Last 24 hours
            </h4>
            {story.current ? (
              <p className="text-body font-semibold">{story.current}</p>
            ) : null}
            {story.day || relation ? (
              <p className="text-muted-foreground text-body">
                {[story.day, relation].filter(Boolean).join(" ")}
              </p>
            ) : null}
            {series?.length ? (
              <UptimeBar
                series={series}
                end={end}
                label={first.shortName}
                showLegend
                size="lg"
              />
            ) : null}
          </div>
        );
    } else {
      const names = affected.map((engine) => engine.shortName);
      facts.push([
        "Engines",
        names.length > 3
          ? `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`
          : names.join(", "),
      ]);
    }
    evidence.push([
      "Failed requests",
      failedRequestsFact(failed, records.length, { who: false }) ?? activityFallback,
    ]);
    return {
      key: single
        ? `engine:${first.service.id}`
        : `engines:${affected.map((e) => e.service.id).join(",")}`,
      severity: key === "down" && exposed.length ? "critical" : "attention",
      tone: exposed.length ? STATE_WORDS[key].tone : "warn",
      // The verdict line above carries the state word, so the title says
      // what it means for people, or the fault when nothing is exposed.
      title: consequence
        ? `${joinNames([exposed[0], ...exposed.slice(1).map((label) => label.toLowerCase())])} ${failed.length ? "requests have failed" : `${exposed.length === 1 ? "is" : "are"} unavailable right now`}`
        : single
          ? symptomOf(first).replace(/\.$/, "")
          : `${affected.length} engines ${symptomOf(first)
              .replace(first.shortName, "")
              .trim()
              .replace(/^is /, "are ")
              .replace(/\.$/, "")}`,
      headline: exposed.length
        ? exposed.length > 2
          ? `${exposed.length} capabilities ${word}`
          : `${joinNames([exposed[0], ...exposed.slice(1).map((label) => label.toLowerCase())])} ${word}`
        : single
          ? `${first.shortName} ${word}`
          : `${affected.length} engines ${word}`,
      facts,
      evidence,
      history,
      body: single ? [engineRemedy(first.lastError)] : undefined,
      href: single
        ? `/engines/?service=${encodeURIComponent(first.service.id)}`
        : "/engines/",
      action: single ? `Inspect ${first.shortName}` : "Inspect engines",
    } satisfies Problem;
  });
}

/**
 * Spending-cap refusals, read from refused receipts' warnings, per access
 * plan. A refusal stops counting once a later paid request on the same plan
 * succeeded, or after 24 hours.
 */
function capProblems(
  records: ActivityRecord[],
  profiles: Profile[] | undefined,
  now: number,
): Problem[] {
  const byPlan = new Map<string, ActivityRecord[]>();
  const resolved = new Set<string>();
  for (const record of records.toSorted(
    (a, b) => (b.created_at ?? 0) - (a.created_at ?? 0),
  )) {
    const plan = record.receipt?.app_profile ?? "unknown";
    if (resolved.has(plan)) continue;
    if (record.receipt?.remote && isSuccess(record.receipt.status)) {
      resolved.add(plan);
      continue;
    }
    const capped =
      String(record.receipt?.status ?? "").toLowerCase() === "rejected" &&
      (record.receipt?.warnings ?? []).some((text) => CAP_WARNING.test(text));
    // A refusal with no time cannot be shown as current.
    if (!capped || !record.created_at) continue;
    if (
      !capRefusalCurrent(
        {
          capability: null,
          status: "rejected",
          at: record.created_at,
          warnings: record.receipt?.warnings,
        },
        now,
      )
    )
      continue;
    byPlan.set(plan, [...(byPlan.get(plan) ?? []), record]);
  }
  return [...byPlan.entries()].map(([planId, refused]) => {
    const profile = profiles?.find((item) => item.id === planId);
    const name = profile?.name ?? "An access plan";
    const remote = (
      profile?.profile as
        | {
            remote?: {
              budget_usd_per_day?: string | number | null;
              budget_usd_per_month?: string | number | null;
            };
          }
        | undefined
    )?.remote;
    const daily =
      remote?.budget_usd_per_day != null
        ? Number(remote.budget_usd_per_day)
        : null;
    const monthly =
      remote?.budget_usd_per_month != null
        ? Number(remote.budget_usd_per_month)
        : null;
    const warning =
      (refused[0].receipt?.warnings ?? []).find((text) =>
        CAP_WARNING.test(text),
      ) ?? "";
    // What the refusals and the plan's own spend record support, and no
    // more (red team round 2): spend per period when the gateway knows it
    // (plan_spend), else "Unavailable"; the refusal's own words, quoted;
    // reset times in local time from the gateway when it gives them.
    const spend =
      (profile as { plan_spend?: PlanSpend[] | null } | undefined)
        ?.plan_spend ?? [];
    const when = (ts: number) =>
      new Date(ts * 1000).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    const per = (period: PlanSpend["period"]) =>
      period === "daily" ? "in the last 24 h" : "this month";
    const facts: Array<[string, ReactNode]> = [
      ["Affects", "Paid models for people on this access plan"],
      [
        "Spent",
        spend.length ? (
          <span key="s" className="block space-y-0.5">
            {spend.map((item) => (
              <span key={item.period} className="block">
                {item.spent_usd != null
                  ? `USD ${Number(item.spent_usd).toFixed(2)} of ${Number(item.cap_usd).toFixed(2)} ${per(item.period)}`
                  : `Unavailable (limit USD ${Number(item.cap_usd).toFixed(2)} ${per(item.period)})`}
              </span>
            ))}
          </span>
        ) : (
          [
            "Unavailable",
            daily != null ? `limit USD ${daily.toFixed(2)} a day` : null,
            monthly != null ? `USD ${monthly.toFixed(2)} a month` : null,
          ]
            .filter(Boolean)
            .join("; ")
        ),
      ],
      ["Gateway said", warning ? `“${warning}”` : "A spending limit"],
      [
        "Resets",
        spend.some((item) => item.resets_at != null) ? (
          <span key="r" className="block space-y-0.5">
            {spend
              .map((item) => (
                <span key={item.period} className="block">
                  {item.period === "daily" ? "Daily" : "Monthly"}:{" "}
                  {item.resets_at != null ? when(item.resets_at) : "Reset time not provided"}
                </span>
              ))}
          </span>
        ) : (
          "Reset time not provided"
        ),
      ],
      ["Refused", failedRequestsFact(refused, records.length)],
    ];
    return {
      key: `cap:${planId}`,
      severity: "attention",
      tone: "warn",
      // The refusals show a limit refused paid requests; they do not show
      // how much was spent.
      title: profile
        ? `A spending limit on the ${name} access plan refused paid requests`
        : "A spending limit refused paid requests",
      headline: profile
        ? `${name} access plan reached its spending limit`
        : "Spending limit reached",
      facts,
      href: "/people?tab=plans",
      action: profile ? "Edit access plan" : "Open access plans",
    } satisfies Problem;
  });
}

function ProblemCard({
  problem,
  primary,
  onDiskHelp,
}: {
  problem: Problem;
  primary: boolean;
  onDiskHelp: (opener: HTMLButtonElement) => void;
}) {
  return (
    <Callout
      as="article"
      variant="incident"
      headingLevel={3}
      severity={problem.severity}
      title={problem.title}
      evidence={problem.evidence}
      history={problem.history}
      actions={
        problem.href || problem.disk || problem.retry ? (
          <>
            {problem.retry ? <Button variant="outline" size="sm" onClick={problem.retry}>Retry</Button> : null}
            {problem.href ? (
              <Button
                asChild
                size={primary ? "md" : "sm"}
                variant={
                  primary && !problem.action?.startsWith("Inspect ")
                    ? "default"
                    : "outline"
                }
              >
                <Link href={problem.href}>
                  {problem.action ?? "Open"}
                  <ChevronRight aria-hidden="true" />
                </Link>
              </Button>
            ) : null}
            {problem.disk ? (
              <Button
                size={primary ? "md" : "sm"}
                variant="outline"
                className="min-h-11 min-w-11"
                onClick={(event) => onDiskHelp(event.currentTarget)}
              >
                Check disk
              </Button>
            ) : null}
          </>
        ) : null
      }
    >
      {problem.facts.length ? (
        <KeyValueList items={problem.facts} className="font-normal" />
      ) : null}
      {problem.body?.map((line) => (
        <p key={line}>{line}</p>
      ))}
    </Callout>
  );
}

function EngineRow({
  engine,
  selected,
  end,
}: {
  engine: EngineView;
  selected: boolean;
  end: number;
}) {
  const [open, setOpen] = useState(selected);
  const series = engine.backend?.history?.status_series;
  const slots = hourSlots(series, end);
  const { up, checked } = hoursUp(slots);
  const key = engineStateKey(engine);
  const { word, tone } = stateWordFor(key);
  return (
    <tr
      className="border-b last:border-0"
      aria-current={selected ? "true" : undefined}
    >
      <th
        scope="row"
        className="py-2.5 pr-4 text-left align-middle font-normal"
      >
        <EngineSheet engine={engine} open={open} onOpenChange={setOpen}>
          <button
            type="button"
            className="block max-w-full min-w-0 truncate text-left text-meta font-medium hover:underline max-sm:min-h-11 max-sm:min-w-11"
            title={engine.name}
          >
            {engine.shortName}
          </button>
        </EngineSheet>
        {engine.label ? (
          <span className="text-muted-foreground hidden truncate text-meta sm:block">
            {engine.label}
          </span>
        ) : null}
      </th>
      <td className="hidden py-2.5 pr-3 align-middle sm:table-cell">
        {engine.service.enabled ? (
          <UptimeBar
            series={series}
            end={end}
            label={engine.shortName}
            showAxis={false}
            hourByHour={false}
          />
        ) : null}
      </td>
      <td className="figures text-muted-foreground hidden py-2.5 pr-4 text-right align-middle text-meta sm:table-cell">
        {engine.service.enabled && checked ? `${up}/${checked} h up` : ""}
      </td>
      <td className="py-2.5 align-middle">
        <StateWord tone={tone} word={word} />
      </td>
    </tr>
  );
}

function money(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value > 0 && value < 0.01
    ? "Less than USD 0.01"
    : `USD ${value.toFixed(2)}`;
}

function providerState(provider: ProviderStatusView): CapabilityStateKey {
  if (provider.enabled === false) return "off";
  if (provider.keyState === "missing") return "unknown";
  if (provider.reachable === false) return "down";
  if (provider.reachable === true) return "ready";
  return "unknown";
}

function ProviderRow({ provider }: { provider: ProviderStatusView }) {
  const [open, setOpen] = useState(false);
  const { word, tone } = provider.keyState === "missing" && provider.enabled !== false
    ? { word: "Needs a key", tone: "warn" as const }
    : stateWordFor(providerState(provider));
  const spent = money(provider.monthlySpend);
  const cap = provider.monthlyCap ?? null;
  const share =
    typeof provider.monthlySpend === "number" &&
    typeof cap === "number" &&
    cap > 0
      ? Math.min(1, provider.monthlySpend / cap)
      : null;
  const spendFallback = !/not measured|unknown/i.test(provider.spendText)
    ? provider.spendText.replace(/\$(\d)/g, "USD $1")
    : null;
  return (
    <tr className="border-b last:border-0">
      <th
        scope="row"
        className="py-2.5 pr-4 text-left align-middle font-normal"
      >
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetTrigger asChild>
            <button type="button" className="block min-h-11 min-w-11 truncate text-left text-meta font-medium hover:underline" aria-label={`${provider.name} details`}>
              {provider.name}
            </button>
          </SheetTrigger>
          <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
            <SheetHeader>
              <SheetTitle>{provider.name}</SheetTitle>
              <SheetDescription>{word}</SheetDescription>
            </SheetHeader>
            <div className="space-y-4 px-4 pb-6">
              <KeyValueList items={[
                // The same evidence as the row: a monthly figure when the
                // provider reports one, else its own spend text, period unstated.
                ...(spent != null
                  ? ([
                      ["Spent this month", spent],
                      ["Monthly cap", money(cap) ?? "Cap not recorded"],
                    ] as Array<[string, ReactNode]>)
                  : ([
                      ["Spend", spendFallback ?? "Spend not recorded"],
                    ] as Array<[string, ReactNode]>)),
                ["Last check", provider.checkedAt ? <RelativeTime ts={provider.checkedAt} /> : "Not checked yet"],
              ]} />
              <Button asChild variant="outline"><Link href="/providers">{provider.keyState === "missing" ? "Add provider key" : "Open providers"}</Link></Button>
            </div>
          </SheetContent>
        </Sheet>
        <span className="text-muted-foreground hidden text-meta sm:block">
          Remote provider
        </span>
      </th>
      <td className="hidden py-2.5 pr-3 align-middle sm:table-cell" colSpan={2}>
        {spent ? (
          <span className="flex items-center gap-3 text-meta">
            {share != null ? (
              <span
                role="img"
                aria-label={`${Math.round(share * 100)}% of this month’s cap used`}
                className="bg-muted relative h-1.5 w-16 shrink-0 overflow-hidden rounded-full"
              >
                <span
                  className={cn(
                    "absolute inset-y-0 left-0 rounded-full",
                    share >= 1
                      ? "bg-danger-fill"
                      : share >= 0.8
                        ? "bg-warning-fill"
                        : "bg-link",
                  )}
                  style={{ width: `${share * 100}%` }}
                />
              </span>
            ) : null}
            <span className="figures whitespace-nowrap">
              {spent}
              {typeof cap === "number" ? ` of ${cap.toFixed(2)}` : ""} this
              month
            </span>
          </span>
        ) : spendFallback ? (
          <span className="figures text-meta">{spendFallback}</span>
        ) : (
          <span className="text-muted-foreground text-meta">
            Spend not recorded
          </span>
        )}
      </td>
      <td className="py-2.5 align-middle">
        <StateWord tone={tone} word={word} />
        <span className="text-muted-foreground block text-meta">
          {provider.checkedAt ? (
            <>
              Checked <RelativeTime ts={provider.checkedAt} fallback="" />
            </>
          ) : (
            "Not checked yet"
          )}
        </span>
      </td>
    </tr>
  );
}

export function StatusClient() {
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const [diskHelpOpen, setDiskHelpOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const diskHelpOpener = useRef<HTMLButtonElement | null>(null);
  const layersSection = useRef<HTMLElement | null>(null);
  const showAllButton = useRef<HTMLButtonElement | null>(null);
  const trackShowAllButton = useCallback((node: HTMLButtonElement | null) => {
    // Only move focus when the removed control actually held it.
    if (!node && showAllButton.current === document.activeElement) {
      layersSection.current?.focus();
    }
    showAllButton.current = node;
  }, []);
  const {
    data: statusData,
    error: statusError,
    isError: statusIsError,
    isFetching: statusIsFetching,
    isPending: statusIsPending,
    refetch: refetchStatus,
  } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    refetchInterval: 30_000,
  });
  const {
    data: registryData,
    error: registryError,
    isError: registryIsError,
    isFetching: registryIsFetching,
    isPending: registryIsPending,
    refetch: refetchRegistry,
  } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });
  const { data: profiles, isPending: profilesIsPending, isError: profilesIsError, refetch: refetchProfiles } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
    retry: false,
  });
  const providers = useOptionalProviderStatus();
  const activity = useActivitySample(true);
  const model = useCapabilityViews(true);
  const records = activity.isPending || activity.isError ? [] : (activity.data ?? []);
  const activityFallback: ReactNode = activity.isPending
    ? "Checking recent requests…"
    : activity.isError || !activity.data
      ? "Recent requests couldn’t load."
      : records.length
        ? `None in the latest ${records.length} requests`
        : "No recent requests";
  const setupEmpty = !statusIsPending && !statusIsError && statusData != null &&
    !registryIsPending && !registryIsError && registryData != null &&
    providers.state === "ready" && providers.providers.length === 0 &&
    Object.keys(registryData.services).length === 0;
  const observedStatus =
    statusIsError || statusData?.current_health?.observation_available !== true
      ? undefined
      : statusData;
  const engines = useMemo(
    () =>
      enginesFrom(
        registryData,
        observedStatus,
        statusIsError ? undefined : statusData,
      ),
    [registryData, observedStatus, statusData, statusIsError],
  );
  const now = Date.now() / 1000;
  const end = statusData?.generated_at ?? now;
  // Remote providers are the ones the provider API lists; registry "kind"
  // marks local engines as providers on real installs, so it is a fallback.
  const providerIds = new Set(
    providers.state === "ready"
      ? providers.providers.map((provider) => provider.id)
      : [],
  );
  const isProviderService = (engine: EngineView) =>
    providers.state === "ready"
      ? providerIds.has(engine.service.id)
      : engine.service.kind.toLowerCase() === "provider" ||
        (engine.service.role || "").toLowerCase() === "provider";
  const isGatewaySelf = (engine: EngineView) =>
    engine.service.kind.toLowerCase() === "gateway" ||
    (engine.service.role || "").toLowerCase() === "gateway" ||
    engine.service.id.toLowerCase() === PRODUCT_NAME.toLowerCase();
  const tableEngines = engines.filter(
    (engine) => !isProviderService(engine) && !isGatewaySelf(engine),
  );
  // Signals are grouped by layer, and disk faults get their own card so
  // the disk remedy never sits beside another fault. Each reason is the
  // gateway's own sentence, shown once.
  const signalGroups = new Map<
    string,
    {
      where: string;
      disk: boolean;
      bad: boolean;
      provider: boolean;
      reasons: Set<string>;
    }
  >();
  for (const signal of statusIsError
    ? []
    : (statusData?.current_health?.signals ?? [])) {
    if (signal.tone !== "warn" && signal.tone !== "bad") continue;
    const hardware = /host|disk|thermal|temperature|cpu|gpu|memory/i.test(
      signal.source,
    );
    const provider = /provider/i.test(signal.source);
    const where = hardware
      ? "The gateway’s computer"
      : provider
        ? "A provider"
        : "The gateway";
    for (const reason of signal.reasons) {
      const disk = /disk/i.test(signal.source) || /\bdisk\b/i.test(reason);
      const key = `${where}:${disk}`;
      const group = signalGroups.get(key) ?? {
        where,
        disk,
        bad: false,
        provider,
        reasons: new Set<string>(),
      };
      group.bad ||= signal.tone === "bad";
      group.reasons.add(reason);
      signalGroups.set(key, group);
    }
  }
  const signalProblems: Problem[] = [...signalGroups.entries()].map(
    ([key, group]) => {
      const title = group.disk
        ? "The gateway’s computer reported a disk problem"
        : `${group.where} needs attention`;
      return {
        key: `signal:${key}`,
        severity: group.bad ? "critical" : "attention",
        tone: group.bad ? "bad" : "warn",
        title,
        headline: title,
        facts: [],
        body: [...group.reasons],
        href: group.provider ? "/providers" : null,
        action: group.provider ? "Open providers" : undefined,
        disk: group.disk,
      } satisfies Problem;
    },
  );
  const providerProblems: Problem[] =
    providers.state === "ready"
      ? providers.providers.flatMap((provider) => {
          if (provider.enabled !== true) return [];
          if (provider.reachable !== false && provider.keyState !== "missing")
            return [];
          const service = engines.find(
            (engine) => engine.service.id === provider.id,
          );
          // A local engine that still serves the same capability keeps it working.
          const labels = service?.label ? service.label.split(", ") : [];
          const covered = labels.filter((label) =>
            tableEngines.some(
              (engine) =>
                engineStateKey(engine) === "ready" &&
                (engine.label ? engine.label.split(", ") : []).includes(label),
            ),
          );
          const exposed = labels.length > covered.length;
          const facts: Array<[string, ReactNode]> = [
            [
              "Affects",
              labels.length
                ? labels
                    .map((label) =>
                      covered.includes(label)
                        ? `${label} (an engine on this computer still serves it)`
                        : label,
                    )
                    .join(", ")
                : "Requests that use this provider",
            ],
            [
              "Last check",
              provider.checkedAt ? (
                <RelativeTime key="c" ts={provider.checkedAt} fallback="" />
              ) : (
                "Not recorded"
              ),
            ],
            [
              "Failed requests",
              failedRequestsFact(
                backendFailures(records, new Set([provider.id])), records.length,
              ) ?? activityFallback,
            ],
          ];
          return [
            {
              key: `provider:${provider.id}`,
              severity: (provider.keyState !== "missing" && (exposed || !labels.length)
                ? "critical"
                : "attention") as CalloutSeverity,
              tone: (provider.keyState !== "missing" && (exposed || !labels.length) ? "bad" : "warn") as StateTone,
              title: provider.keyState === "missing" ? `${provider.name} needs a key.` : `${provider.name} is down`,
              headline: provider.keyState === "missing" ? `${provider.name} needs a key.` : `${provider.name} down`,
              facts,
              body: provider.keyState === "missing" ? [] : ["The last connection check failed."],
              href: "/providers",
              action: provider.keyState === "missing" ? "Add provider key" : `Open ${provider.name}`,
            } satisfies Problem,
          ];
        })
      : [];
  const loadProblems: Problem[] = [
    ...((statusIsError || (!statusIsPending && statusData == null))
      ? [
          {
            key: "status",
            severity: "critical" as CalloutSeverity,
            tone: "bad" as StateTone,
            title: "Engine health couldn’t load",
            headline: "Couldn’t check status",
            facts: [],
            body: [!statusIsError ? "The gateway returned no status. Try again." : statusError instanceof ApiError ? "The gateway returned an error. Try again." : "We couldn’t tell where this stopped. Try again."],
            retry: () => void refetchStatus(),
            href: null,
          },
        ]
      : []),
    ...(registryIsError
      ? [
          {
            key: "registry",
            severity: "critical" as CalloutSeverity,
            tone: "bad" as StateTone,
            title: "Engine list couldn’t load",
            headline: "Couldn’t load your engines",
            facts: [],
            body: [registryError instanceof ApiError ? "The gateway returned an error. Try again." : "We couldn’t tell where this stopped. Try again."],
            retry: () => void refetchRegistry(),
            href: null,
          },
        ]
      : []),
  ];
  // An engine in use whose checks stopped answering: its capabilities read
  // Unknown, so the owner needs to know which engine and since when.
  const uncheckedProblems: Problem[] = statusIsError
    ? []
    : tableEngines.flatMap((engine) => {
        if (!engine.service.enabled || engine.health?.in_use === false)
          return [];
        if (engineStateKey(engine) !== "unknown" || !engine.health) return [];
        // The poller's last record; the type predates this field.
        const observed = (
          engine.backend as { observed_at_s?: number | null } | undefined
        )?.observed_at_s;
        // Only a recorded check that has gone stale; an engine the gateway
        // never checks is Unknown in the table, not "not answering".
        if (typeof observed !== "number" || now - observed <= FRESH_S)
          return [];
        const last = observed;
        return [
          {
            key: `unchecked:${engine.service.id}`,
            severity: "attention" as CalloutSeverity,
            tone: "unknown" as StateTone,
            // What the data shows: no answered check for this long. Not
            // "is not answering", which claims more (red team round 2).
            title: `${engine.shortName} has not answered a check in ${ageText(now - last).replace(/ ago$/, "")}`,
            headline: `${engine.shortName} unchecked`,
            facts: [["Affects", engine.label || "Unknown"]] as Array<
              [string, ReactNode]
            >,
            body: [
              "Its capabilities read Unknown until it answers.",
              "Check that the engine is running and its health address answers.",
            ],
            href: `/engines/?service=${encodeURIComponent(engine.service.id)}`,
            action: `Inspect ${engine.shortName}`,
          } satisfies Problem,
        ];
      });
  const problems = [
    ...loadProblems,
    ...engineProblems(
      engines.filter(
        (engine) =>
          !isGatewaySelf(engine) &&
          (providers.state !== "ready" || !isProviderService(engine)),
      ),
      records,
      end,
      activityFallback,
    ),
    ...uncheckedProblems,
    // Only once checks have loaded: while loading, everything is Unknown.
    ...(statusIsError ||
    model.isPending ||
    !observedStatus?.current_health?.observation_available
      ? []
      : unexplainedProblems(
          model.views,
          [...engines, ...tableEngines].filter((engine) =>
            ["down", "degraded", "unknown"].includes(engineStateKey(engine)),
          ),
        )),
    ...providerProblems,
    ...capProblems(records, profilesIsError ? undefined : profiles, now),
    ...signalProblems,
  ];
  const running = records.filter((record) =>
    isInFlight(record.receipt?.status),
  );
  const health = observedStatus?.current_health;
  const incomplete = Object.values(health?.incomplete ?? {}).some(
    (gaps) => gaps?.length,
  );
  let verdict: { tone: StateTone; text: string };
  let note: string | null = null;
  // The capability model speaks first, so Status, Home and the shell agree.
  // Engine, provider and signal problems the model cannot see come next.
  const modelReady =
    !model.isPending && !model.isError && model.views.length > 0;
  if (registryIsPending || statusIsPending) {
    verdict = { tone: "unknown", text: "Checking" };
  } else if (statusIsError || registryIsError || statusData == null) {
    verdict = { tone: "bad", text: "Couldn’t check status" };
  } else if (
    setupEmpty && statusData?.current_health?.state === "empty"
  ) {
    verdict = { tone: "off", text: "No engines yet" };
  } else if (!health) {
    verdict = { tone: "unknown", text: "Engine health unknown" };
    note = "The gateway has not reported a current check.";
  } else if (modelReady && model.verdict.problems.length) {
    verdict = { tone: model.verdict.tone, text: model.verdict.text };
  } else if (problems.length === 1) {
    verdict = { tone: problems[0].tone, text: problems[0].headline };
  } else if (problems.length > 1) {
    verdict = { tone: "warn", text: `${problems.length} problems need you` };
  } else if (providers.state === "loading" || model.isPending) {
    verdict = { tone: "unknown", text: "Checking" };
  } else if (incomplete || health.state !== "ok") {
    // Unchecked engines come before provider trouble: neither may read as working.
    verdict = { tone: "unknown", text: "Some engines are unchecked" };
  } else if (providers.state === "error") {
    verdict = { tone: "unknown", text: "Provider status unavailable" };
  } else if (!modelReady) {
    verdict = { tone: "unknown", text: "Capability states unknown" };
    note = model.isError
      ? "Your capabilities did not load."
      : "The gateway reported no capabilities.";
  } else {
    verdict = { tone: model.verdict.tone, text: model.verdict.text };
  }
  if (providers.state === "error") note = "Provider status did not load.";
  // While something needs attention, healthy engines fold to one line
  // (principle 7 ranks above filling space; D1.5b).
  const tableOpen = showAll || !problems.length;
  const capsOpen = !problems.length;
  // Engines that already have a card are not counted again in the summary.
  const troubled = new Set(
    problems.flatMap((problem) => {
      const [kind, ids = ""] = problem.key.split(":");
      return ["engine", "engines", "unchecked"].includes(kind)
        ? ids.split(",")
        : [];
    }),
  );
  const others = tableEngines.filter(
    (engine) => !troubled.has(engine.service.id),
  );
  const count = (key: CapabilityStateKey) =>
    others.filter((engine) => engineStateKey(engine) === key).length;
  const providerList = providers.state === "ready" ? providers.providers : [];
  const providersDown = providerList.filter(
    (provider) => providerState(provider) === "down",
  ).length;
  const providersKeyless = providerList.filter(
    (provider) => provider.enabled !== false && provider.keyState === "missing",
  ).length;
  // The folded engines and providers as two labelled lines (D1.3 found
  // "4 other engines ready, 1 off, 1 provider" cryptic; D1.4 found the
  // sentences that replaced it read as prose): "Other engines: 4 ready,
  // 1 turned off" and "Remote providers: 1 set up".
  const engineParts = (
    [
      ["ready", "ready"],
      ["busy", "busy"],
      ["degraded", "degraded"],
      ["down", "down"],
      ["unknown", "not checked"],
      ["off", "turned off"],
    ] as Array<[CapabilityStateKey, string]>
  )
    .map(([key, words]) => [count(key), words] as const)
    .filter(([n]) => n > 0);
  const summaryLines: Array<[string, string]> = [
    ...(engineParts.length
      ? [
          [
            troubled.size ? "Other engines" : "Engines",
            engineParts.map(([n, words]) => `${n} ${words}`).join(", "),
          ] as [string, string],
        ]
      : []),
    ...(providerList.length
      ? [
          [
            "Remote providers",
            `${providerList.length} set up${providersKeyless ? `, ${providersKeyless} ${providersKeyless === 1 ? "needs" : "need"} a key` : ""}${providersDown ? `, ${providersDown} down` : ""}`,
          ] as [string, string],
        ]
      : []),
  ];
  // A provider-status failure and its retry stay visible even while the
  // table is folded: an error is not healthy detail (principle 7).
  const providerError =
    providers.state === "error" ? (
      <div role="alert" className="text-destructive text-meta">
        Provider status couldn’t load.{" "}
        <button
          type="button"
          onClick={providers.refetch}
          className="text-link inline-flex min-h-11 min-w-11 items-center font-medium underline underline-offset-4"
        >
          Retry providers
        </button>
      </div>
    ) : null;
  const refreshAll = () => {
    void refetchStatus();
    void refetchRegistry();
    providers.refetch();
    model.refetch();
    void activity.refetch();
    void refetchProfiles();
  };

  return (
    // A reading column, like Linear and incident.io status pages (about 670 to
    // 900 px), so facts and actions stay within one eye span.
    // D1.5b: one column at every width, problem or not; no side column
    // to stand empty (Linear and incident.io incident pages).
    <div className="page-read min-w-0">
      <PageHeader
        title="Status"
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={refreshAll}
              disabled={statusIsFetching || registryIsFetching}
              aria-label="Refresh"
            >
              <RefreshCcw aria-hidden="true" />
              <span className="max-sm:sr-only">
                {statusIsFetching ? "Refreshing…" : "Refresh"}
              </span>
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Status actions"
                >
                  <MoreHorizontal className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem asChild>
                  <a href="/status/raw">
                    <Server className="size-4" /> Technical details
                  </a>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        }
      >
        <StatusLine
          tone={verdict.tone}
          text={verdict.text}
          unconfirmed={model.verdict.unconfirmed}
          checkedAt={
            !statusIsError && health && model.verdict.checkedAt
              ? model.verdict.checkedAt
              : null
          }
        >
          {note ? <span>{note}</span> : null}
        </StatusLine>
      </PageHeader>
      {/* While a card explains a problem, a wide screen keeps the card in a
          reading column and puts the rest of the system beside it (D1.3:
          the right third stood empty). */}
      <div className="flex min-w-0 flex-col gap-(--space-section)">
        {statusIsPending && registryIsPending ? (
          <div aria-busy="true" className="space-y-4">
            <Skeleton className="rounded-panel h-40 w-full" />
            <Skeleton className="rounded-panel h-64 w-full" />
          </div>
        ) : (
          <>
            {problems.length ? (
              <section
                aria-labelledby="attention-heading"
                className="space-y-3"
              >
                <SectionHeading
                  id="attention-heading"
                  title="Needs attention"
                />
                <div className="grid gap-3">
                  {problems.map((problem, index) => (
                    <ProblemCard
                      key={problem.key}
                      problem={problem}
                      primary={index === 0}
                      onDiskHelp={(opener) => {
                        diskHelpOpener.current = opener;
                        setDiskHelpOpen(true);
                      }}
                    />
                  ))}
                </div>
              </section>
            ) : null}

            {profilesIsError ? (
              <div role="alert" className="text-destructive flex flex-wrap items-center gap-2 text-meta">
                Access plan details couldn’t load.
                <Button variant="outline" size="sm" onClick={() => void refetchProfiles()}>Retry access plans</Button>
              </div>
            ) : profilesIsPending && records.some((record) => (record.receipt?.warnings ?? []).some((warning) => CAP_WARNING.test(warning))) ? (
              <p className="text-muted-foreground text-meta">Checking access plan details…</p>
            ) : null}
            {activity.isPending ? (
              <p className="text-muted-foreground text-meta">Checking recent requests…</p>
            ) : activity.isError || !activity.data ? (
              <div role="alert" className="text-destructive flex flex-wrap items-center gap-2 text-meta">
                Recent requests couldn’t load.
                <Button variant="outline" size="sm" onClick={() => void activity.refetch()}>Retry recent requests</Button>
              </div>
            ) : null}
            {model.isError ? (
              <div role="alert" className="text-destructive flex flex-wrap items-center gap-2 text-meta">
                Capabilities couldn’t load.
                <Button variant="outline" size="sm" onClick={model.refetch}>Retry capabilities</Button>
              </div>
            ) : model.isPending ? (
              <p className="text-muted-foreground text-meta">Checking capabilities…</p>
            ) : null}
            {running.length ? (
              <section aria-labelledby="running-heading">
                <SectionHeading
                  id="running-heading"
                  title="Running now"
                  count={running.length}
                />
                <ul>
                  {running.slice(0, 5).map((record) => {
                    const capability = requestCapability(
                      record.receipt?.intent,
                      record.receipt?.path,
                    );
                    return (
                      <li
                        key={record.receipt?.request_id}
                        className="grid min-h-8 max-sm:min-h-11 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 border-b py-2 text-meta"
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          {record.person_label ? (
                            <PersonMark name={record.person_label} size="xs" />
                          ) : null}
                          <span className="truncate">
                            {capability
                              ? capabilityLabel(capability)
                              : "Request"}
                            {record.person_label
                              ? ` for ${record.person_label}`
                              : ""}
                          </span>
                        </span>
                        <StateWord tone="busy" word="In progress" />
                      </li>
                    );
                  })}
                </ul>
              </section>
            ) : null}

            <div className="flex min-w-0 flex-col gap-(--space-section) xl:contents">
              {/* Capability states lean on engine checks; without a current
              check they cannot vouch for anything, so they stay hidden. */}
              {health && modelReady ? (
                <section aria-labelledby="capability-strip-heading">
                  <SectionHeading
                    id="capability-strip-heading"
                    title="Capabilities"
                  />
                  {capsOpen ? (
                    <CapabilityList
                      // A failing engine already has a callout with its symptom.
                      views={model.views.map((view) =>
                        view.failing.length ? { ...view, detail: null } : view,
                      )}
                      columns={2}
                    />
                  ) : (
                    <CapabilityChips views={model.views} />
                  )}
                </section>
              ) : null}

              <section
                ref={layersSection}
                tabIndex={-1}
                aria-labelledby="layers-heading"
              >
                <SectionHeading
                  id="layers-heading"
                  title="Engines and providers"
                />
                {providers.state === "loading" ? <p aria-live="polite" className="text-muted-foreground text-meta">Checking providers…</p> : null}
                {/* Rows need both answers: drawn before the status check
                    returns, every engine would read "Unknown" for a moment. */}
                {registryIsPending || statusIsPending ? (
                  <Skeleton
                    aria-busy="true"
                    aria-label="Checking engines"
                    className="rounded-panel h-48 w-full"
                  />
                ) : setupEmpty ? (
                  <Callout
                    severity="info"
                    title="No engines yet"
                    actions={
                      <Button asChild size="sm">
                        <Link href="/engines">Add an engine</Link>
                      </Button>
                    }
                  >
                    <p className="text-muted-foreground">
                      Connect an engine on this computer, or a provider you pay
                      for.
                    </p>
                    {providerError}
                  </Callout>
                ) : tableOpen ? (
                  <div className="bg-card rounded-panel border px-4 sm:px-5">
                    <table className="w-full table-fixed">
                      <caption className="sr-only">Engine status</caption>
                      <thead>
                        <tr className="text-subtle-foreground border-b text-left text-meta">
                          <th scope="col" className="py-2 font-normal sm:w-52">
                            Engine
                          </th>
                          <th
                            scope="col"
                            className="hidden py-2 font-normal sm:table-cell"
                          >
                            Health checks, last 24 h
                          </th>
                          <th
                            scope="col"
                            className="hidden w-24 py-2 text-right font-normal sm:table-cell"
                          >
                            <span className="sr-only">Hours up</span>
                          </th>
                          <th scope="col" className="w-32 py-2 font-normal">
                            State
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {tableEngines.map((engine) => (
                          <EngineRow
                            key={engine.service.id}
                            engine={engine}
                            selected={engine.service.id === selectedServiceId}
                            end={end}
                          />
                        ))}
                        {providerList.map((provider) => (
                          <ProviderRow key={provider.id} provider={provider} />
                        ))}
                      </tbody>
                    </table>
                    {providerError ? (
                      <div className="border-t py-3">{providerError}</div>
                    ) : providers.state === "ready" && !providerList.length ? (
                      // The provider layer's empty state, with its next action.
                      <p className="text-muted-foreground flex flex-wrap items-center gap-x-3 border-t py-3 text-meta">
                        <span>No remote providers are configured.</span>
                        <Link
                          href="/engines"
                          className="text-link font-medium underline-offset-4 hover:underline max-sm:inline-block max-sm:py-3"
                        >
                          Review connections
                        </Link>
                      </p>
                    ) : null}
                  </div>
                ) : (
                  <>
                    <button
                      ref={trackShowAllButton}
                      type="button"
                      onClick={() => setShowAll(true)}
                      className="bg-card hover:border-input rounded-panel flex min-h-8 max-sm:min-h-11 w-full flex-wrap items-center justify-between gap-x-3 gap-y-1 border px-5 py-3 text-left text-meta"
                    >
                      <span className="grid min-w-0 gap-0.5">
                        {summaryLines.length
                          ? summaryLines.map(([label, value]) => (
                              <span key={label}>
                                <span className="text-muted-foreground">
                                  {label}:
                                </span>{" "}
                                {value}.
                              </span>
                            ))
                          : "Engines and providers"}
                      </span>
                      <span className="text-link inline-flex shrink-0 items-center gap-1 font-medium whitespace-nowrap">
                        Show all
                        <ChevronRight aria-hidden="true" className="size-4" />
                      </span>
                    </button>
                    {providerError ? (
                      <div className="mt-3">{providerError}</div>
                    ) : null}
                  </>
                )}
              </section>
            </div>
          </>
        )}
        <Sheet open={diskHelpOpen} onOpenChange={setDiskHelpOpen}>
          <SheetContent
            side="right"
            className="w-full overflow-y-auto sm:max-w-lg"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              if (diskHelpOpen) return;
              const opener = diskHelpOpener.current;
              if (opener?.isConnected && !opener.disabled) opener.focus();
              else layersSection.current?.focus();
            }}
          >
            <SheetHeader>
              <SheetTitle>Check disk</SheetTitle>
              <SheetDescription>
                Inspect the disk and its reported error before removing anything.
              </SheetDescription>
            </SheetHeader>
            <ul className="list-disc space-y-3 px-8 py-4 text-meta">
              <li>
                Review old filesystem snapshots and remove only snapshots you no
                longer need.
              </li>
              <li>
                Check unused Docker images, containers, and volumes before
                removing them.
              </li>
              <li>
                Clear regenerable model, build, and application caches that you
                recognize.
              </li>
            </ul>
          </SheetContent>
        </Sheet>
      </div>
    </div>
  );
}
