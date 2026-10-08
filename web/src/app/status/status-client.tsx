"use client";

import Link from "next/link";
import { useMemo, useState, type ReactNode } from "react";
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
import { SectionHeading } from "@/components/product/readiness";
import {
  Callout,
  CapabilityList,
  StateWord,
  StatusLine,
  type CalloutSeverity,
} from "@/components/product/status-ui";
import {
  failingSince,
  hourSlots,
  hoursUp,
  UptimeBar,
} from "@/components/product/uptime-bar";
import { PageHeader } from "@/components/product/page-header";
import { PersonMark } from "@/components/product/person-mark";
import {
  isInFlight,
  isSuccess,
  CAP_WARNING,
  capRefusalCurrent,
  engineRemedy,
  engineSymptom,
  isTerminalFailure,
  requestCapability,
  STATE_WORDS,
  type CapabilityStateKey,
  type StateTone,
} from "@/lib/capability-state";
import {
  ACTIVITY_SAMPLE,
  capabilityLabel,
  useActivitySample,
  useCapabilityViews,
  type ActivityRecord,
} from "@/lib/use-capability-views";
import { capLimitFrom } from "@/app/home/presentation";

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
        label:
          display?.capability_labels
            ?.filter((label) => !/\((?:speech|transcription)\)/i.test(label))
            .join(", ") || "",
        role: service.role || h?.role || titleize(service.kind || "engine"),
        state: healthPresentation.label,
        tone: healthPresentation.tone,
        endpoint: endpoint(service, backend),
        models: serviceModels(service),
        queue: valueText(backendExtras?.queue ?? backendExtras?.queue_depth),
        lastError: h?.last_error ?? backend?.last_error ?? null,
        checkedAt: presentationStatus?.generated_at ?? null,
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
          <dl className="space-y-3 text-sm">
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
            <summary className="min-h-11 cursor-pointer py-3 text-sm">
              Technical fields
            </summary>
            <pre className="bg-muted rounded-control max-h-96 overflow-auto p-3 text-xs">
              {JSON.stringify(engine.service, null, 2)}
            </pre>
          </details>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
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

function failedRequestsFact(records: ActivityRecord[]): ReactNode {
  if (!records.length) return null;
  const people = [
    ...new Set(records.map((record) => record.person_label).filter(Boolean)),
  ] as string[];
  const latest = records[0];
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      {people.slice(0, 4).map((name) => (
        <PersonMark key={name} name={name} size="xs" label={name} />
      ))}
      <span>
        {records.length} of the latest {ACTIVITY_SAMPLE} requests
        {people.length ? `, from ${joinNames(people)}` : ""}; latest{" "}
        <RelativeTime ts={latest.created_at ?? null} fallback="" />
      </span>
      {latest.receipt?.request_id ? (
        <a
          href={`/r/${encodeURIComponent(latest.receipt.request_id)}`}
          className="text-link inline-flex min-h-11 items-center font-medium underline-offset-4 hover:underline sm:min-h-0"
        >
          Open receipt
        </a>
      ) : null}
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
  /** Plain paragraphs: the symptom, then the fix. */
  body?: string[];
  href?: string | null;
  action?: string;
  disk?: boolean;
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
    const facts: Array<[string, ReactNode]> = [
      [
        "Affects",
        [
          ...exposed,
          ...covered.map(
            (label) => `${label} (another engine still serves it)`,
          ),
        ].join(", ") || "Unknown",
      ],
    ];
    if (affected.length === 1) {
      const series = first.backend?.history?.status_series;
      const slots = hourSlots(series, end);
      const { up, checked } = hoursUp(slots);
      const since = failingSince(slots);
      if (since) facts.push(["Failing since", `The hour from ${clock(since)}`]);
      if (series?.length)
        facts.push([
          "Health checks",
          <div key="u" className="max-w-md space-y-1">
            <span>
              {up} of {checked} checked hours up, last 24 h
            </span>
            <UptimeBar series={series} end={end} label={first.shortName} />
          </div>,
        ]);
    } else {
      facts.push([
        "Engines",
        affected.map((engine) => engine.shortName).join(", "),
      ]);
    }
    facts.push([
      "Failed requests",
      failedRequestsFact(failed) ??
        `None in the latest ${ACTIVITY_SAMPLE} requests`,
    ]);
    const single = affected.length === 1;
    return {
      key: single
        ? `engine:${first.service.id}`
        : `engines:${affected.map((e) => e.service.id).join(",")}`,
      severity: key === "down" && exposed.length ? "critical" : "attention",
      tone: exposed.length ? STATE_WORDS[key].tone : "warn",
      title: single
        ? `${first.shortName} is ${word}`
        : `${affected.length} engines are ${word}`,
      headline: exposed.length
        ? exposed.length > 2
          ? `${exposed.length} capabilities ${word}`
          : `${joinNames([exposed[0], ...exposed.slice(1).map((label) => label.toLowerCase())])} ${word}`
        : single
          ? `${first.shortName} ${word}`
          : `${affected.length} engines ${word}`,
      facts,
      body: single
        ? [symptomOf(first), engineRemedy(first.lastError)]
        : [
            `${affected.length} engines ${symptomOf(first).replace(first.shortName, "").trim().replace(/^is /, "are ")}`,
          ],
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
  const month = new Date(now * 1000);
  const resets = new Date(
    Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 1),
  ).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
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
    const limit = capLimitFrom(warning);
    const facts: Array<[string, ReactNode]> = [
      [
        "Limits",
        [
          daily != null ? `USD ${daily.toFixed(2)} a day (rolling 24 h)` : null,
          monthly != null ? `USD ${monthly.toFixed(2)} a month` : null,
        ]
          .filter(Boolean)
          .join(", ") || "Not recorded",
      ],
      ["Reached", warning || "A spending limit"],
      [
        "Resets",
        limit === "monthly"
          ? `Monthly limit on ${resets} (UTC)`
          : "As charges older than 24 hours roll off",
      ],
      ["Refused", failedRequestsFact(refused)],
    ];
    return {
      key: `cap:${planId}`,
      severity: "attention",
      tone: "warn",
      title: profile
        ? `The ${name} plan reached its spending limit`
        : "An access plan reached its spending limit",
      headline: profile
        ? `${name} plan reached its spending limit`
        : "Spending limit reached",
      facts,
      href: "/people?tab=plans",
      action: profile ? `Edit the ${name} plan` : "Open access plans",
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
  onDiskHelp: () => void;
}) {
  return (
    <Callout
      as="article"
      headingLevel={3}
      split
      severity={problem.severity}
      title={problem.title}
      facts={problem.facts}
      actions={
        problem.href || problem.disk ? (
          <>
            {problem.href ? (
              <Button
                asChild
                variant={primary ? "default" : "outline"}
                size="sm"
              >
                <Link href={problem.href}>
                  {problem.action ?? "Open"}
                  <ChevronRight aria-hidden="true" />
                </Link>
              </Button>
            ) : null}
            {problem.disk ? (
              <Button variant="outline" size="sm" onClick={onDiskHelp}>
                Free disk space
              </Button>
            ) : null}
          </>
        ) : null
      }
    >
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
      <th scope="row" className="py-2 pr-4 text-left align-middle font-normal">
        <EngineSheet engine={engine} open={open} onOpenChange={setOpen}>
          <button
            type="button"
            className="block max-w-full min-w-0 truncate py-1.5 text-left text-sm font-medium hover:underline max-sm:min-h-11"
            title={engine.name}
          >
            {engine.shortName}
          </button>
        </EngineSheet>
      </th>
      <td className="text-muted-foreground hidden py-2 pr-4 align-middle text-sm md:table-cell">
        {engine.label || null}
      </td>
      <td className="hidden w-52 py-2 pr-3 align-middle sm:table-cell">
        {engine.service.enabled ? (
          <UptimeBar
            series={series}
            end={end}
            label={engine.shortName}
            showAxis={false}
          />
        ) : null}
      </td>
      <td className="figures text-muted-foreground hidden w-20 py-2 pr-4 text-right align-middle text-xs sm:table-cell">
        {engine.service.enabled && checked ? `${up}/${checked} h up` : ""}
      </td>
      <td className="py-2 align-middle">
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
  if (provider.keyState === "missing" || provider.reachable === false)
    return "down";
  if (provider.reachable === true) return "ready";
  return "unknown";
}

function ProviderRow({ provider }: { provider: ProviderStatusView }) {
  const { word, tone } = stateWordFor(providerState(provider));
  const spent = money(provider.monthlySpend);
  const cap = provider.monthlyCap ?? null;
  const share =
    typeof provider.monthlySpend === "number" &&
    typeof cap === "number" &&
    cap > 0
      ? Math.min(1, provider.monthlySpend / cap)
      : null;
  return (
    <tr className="border-b last:border-0">
      <th
        scope="row"
        className="py-2.5 pr-4 text-left align-middle text-sm font-medium"
      >
        {provider.name}
      </th>
      <td className="text-muted-foreground hidden py-2.5 pr-4 align-middle text-sm md:table-cell">
        Provider
      </td>
      <td className="hidden py-2.5 pr-3 align-middle sm:table-cell" colSpan={2}>
        {spent ? (
          <span className="flex items-center gap-3 text-sm">
            {share != null ? (
              <span
                role="img"
                aria-label={`${Math.round(share * 100)}% of this month’s cap used`}
                className="bg-muted relative h-1.5 w-24 shrink-0 overflow-hidden rounded-full"
              >
                <span
                  className={cn(
                    "absolute inset-y-0 left-0 rounded-full",
                    share >= 1
                      ? "bg-danger"
                      : share >= 0.8
                        ? "bg-warning"
                        : "bg-link",
                  )}
                  style={{ width: `${share * 100}%` }}
                />
              </span>
            ) : null}
            <span className="figures">
              {spent}
              {typeof cap === "number" ? ` of USD ${cap.toFixed(2)}` : ""} this
              month
            </span>
          </span>
        ) : !/not measured|unknown/i.test(provider.spendText) ? (
          <span className="figures text-sm">
            {provider.spendText.replace(/\$(\d)/g, "USD $1")}
          </span>
        ) : (
          <span className="text-muted-foreground text-sm">
            Spend not recorded
          </span>
        )}
      </td>
      <td className="py-2.5 align-middle">
        <StateWord tone={tone} word={word} />
        <span className="text-muted-foreground block text-xs">
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
  const {
    data: statusData,
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
  const { data: profiles } = useQuery({
    queryKey: ["status", "profiles"],
    queryFn: api.profiles.list,
    retry: false,
  });
  const providers = useOptionalProviderStatus();
  const activity = useActivitySample(true);
  const model = useCapabilityViews(true);
  const records = activity.data ?? [];
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
        ? `${group.where} is low on disk space`
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
          const facts: Array<[string, ReactNode]> = [
            ["Affects", service?.label || "Requests that use this provider"],
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
                backendFailures(records, new Set([provider.id])),
              ) ?? `None in the latest ${ACTIVITY_SAMPLE} requests`,
            ],
          ];
          return [
            {
              key: `provider:${provider.id}`,
              severity: "critical" as CalloutSeverity,
              tone: "bad" as StateTone,
              title: `${provider.name} is down`,
              headline: `${provider.name} down`,
              facts,
              body: [
                provider.keyState === "missing"
                  ? "No provider key is set."
                  : "The last connection check failed.",
              ],
              href: "/providers",
              action: `Open ${provider.name}`,
            } satisfies Problem,
          ];
        })
      : [];
  const loadProblems: Problem[] = [
    ...(statusIsError
      ? [
          {
            key: "status",
            severity: "critical" as CalloutSeverity,
            tone: "bad" as StateTone,
            title: "The gateway did not return engine health",
            headline: "Couldn’t check status",
            facts: [["Where", "The gateway"]] as Array<[string, ReactNode]>,
            body: ["Refresh to try again."],
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
            title: "The gateway did not return the engine list",
            headline: "Couldn’t load your engines",
            facts: [["Where", "The gateway"]] as Array<[string, ReactNode]>,
            body: [
              registryError instanceof ApiError ||
              registryError instanceof Error
                ? registryError.message
                : "Refresh to try again.",
            ],
            href: null,
          },
        ]
      : []),
  ];
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
    ),
    ...providerProblems,
    ...capProblems(records, profiles, now),
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
  } else if (statusIsError || registryIsError) {
    verdict = { tone: "bad", text: "Couldn’t check status" };
  } else if (
    statusData?.current_health?.state === "empty" &&
    engines.length === 0
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
  const tableOpen = showAll || !problems.length;
  const troubled = new Set(
    problems.flatMap((problem) =>
      problem.key.startsWith("engine:") ? [problem.key.slice(7)] : [],
    ),
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
  const foldedSummary = [
    count("ready")
      ? `${count("ready")} other engine${count("ready") === 1 ? "" : "s"} ready`
      : null,
    count("off") ? `${count("off")} off` : null,
    count("unknown") ? `${count("unknown")} unknown` : null,
    providerList.length
      ? `${providerList.length} provider${providerList.length === 1 ? "" : "s"}${providersDown ? ` (${providersDown} down)` : ""}`
      : null,
  ]
    .filter(Boolean)
    .join(", ");
  const refreshAll = () => {
    void refetchStatus();
    void refetchRegistry();
    providers.refetch();
    model.refetch();
  };

  return (
    <div className="min-w-0">
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
          checkedAt={
            !statusIsError && health && model.verdict.checkedAt
              ? model.verdict.checkedAt
              : null
          }
        >
          {note ? <span>{note}</span> : null}
        </StatusLine>
      </PageHeader>
      <div className="flex min-w-0 flex-col gap-10">
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
                      onDiskHelp={() => setDiskHelpOpen(true)}
                    />
                  ))}
                </div>
              </section>
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
                        className="grid min-h-11 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 border-b py-2 text-sm"
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
                        <StateWord tone="busy" word="Running" />
                      </li>
                    );
                  })}
                </ul>
              </section>
            ) : null}

            {/* Capability states lean on engine checks; without a current
              check they cannot vouch for anything, so they stay hidden. */}
            {health && modelReady ? (
              <section aria-labelledby="capability-strip-heading">
                <SectionHeading
                  id="capability-strip-heading"
                  title="Capabilities"
                />
                <CapabilityList
                  // A failing engine already has a callout with its symptom.
                  views={model.views.map((view) =>
                    view.failing.length ? { ...view, detail: null } : view,
                  )}
                  columns={2}
                />
              </section>
            ) : null}

            <section aria-labelledby="layers-heading">
              <SectionHeading
                id="layers-heading"
                title="Engines and providers"
              />
              {registryIsPending ? (
                <Skeleton className="rounded-panel h-48 w-full" />
              ) : !tableEngines.length && providers.state !== "ready" ? (
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
                </Callout>
              ) : tableOpen ? (
                <div className="bg-card rounded-panel border px-4 sm:px-5">
                  <table className="w-full table-fixed">
                    <caption className="sr-only">Engine status</caption>
                    <thead>
                      <tr className="text-subtle-foreground border-b text-left text-xs">
                        <th scope="col" className="py-2 font-normal">
                          Engine
                        </th>
                        <th
                          scope="col"
                          className="hidden py-2 font-normal md:table-cell"
                        >
                          Serves
                        </th>
                        <th
                          scope="col"
                          className="hidden w-52 py-2 font-normal sm:table-cell"
                        >
                          Health checks, last 24 h
                        </th>
                        <th
                          scope="col"
                          className="hidden w-20 py-2 text-right font-normal sm:table-cell"
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
                  {providers.state === "error" ? (
                    <div
                      role="alert"
                      className="text-destructive border-t py-3 text-sm"
                    >
                      Provider status couldn’t load: {providers.message}{" "}
                      <button
                        type="button"
                        onClick={providers.refetch}
                        className="text-link font-medium underline underline-offset-4"
                      >
                        Retry providers
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setShowAll(true)}
                  className="bg-card hover:border-input rounded-panel flex min-h-11 w-full flex-wrap items-center justify-between gap-x-3 gap-y-1 border px-5 py-3 text-left text-sm"
                >
                  <span>{foldedSummary || "Engines and providers"}</span>
                  <span className="text-link inline-flex shrink-0 items-center gap-1 font-medium whitespace-nowrap">
                    Show all
                    <ChevronRight aria-hidden="true" className="size-4" />
                  </span>
                </button>
              )}
            </section>
          </>
        )}
        <Sheet open={diskHelpOpen} onOpenChange={setDiskHelpOpen}>
          <SheetContent
            side="right"
            className="w-full overflow-y-auto sm:max-w-lg"
          >
            <SheetHeader>
              <SheetTitle>Free disk space</SheetTitle>
              <SheetDescription>
                Check the largest storage areas first.
              </SheetDescription>
            </SheetHeader>
            <ul className="list-disc space-y-3 px-8 py-4 text-sm">
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
