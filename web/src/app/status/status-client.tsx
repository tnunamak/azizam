"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, MoreHorizontal, RefreshCcw, Server } from "lucide-react";

import {
  api,
  ApiError,
  queryKeys,
  type CurrentHealthService,
  type Service,
  type ServiceRegistry,
  type StatusPayload,
} from "@/lib/api";
import { productApi } from "@/lib/product-api";
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
import {
  availabilityState,
  capabilityMeta,
  SectionHeading,
  StateLabel,
  Verdict,
  type StateView,
  type Tone,
} from "@/components/product/readiness";
import {
  hoursUnhealthy,
  UptimeBar,
  uptimePercent,
} from "@/components/product/uptime-bar";
import { PersonMark } from "@/components/product/person-mark";

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
  /** When the status payload behind this row was generated (epoch seconds). */
  checkedAt: number | null;
};

function titleize(value: string): string {
  return value
    .replace(/[._-]/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function lowerFirst(value: string): string {
  return value ? value.charAt(0).toLowerCase() + value.slice(1) : value;
}
function valueText(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "number")
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong. Try again.";
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

/** The symptom in plain words; a raw transport error stays in the details. */
function engineSymptom(engine: EngineView): string {
  const raw = (engine.lastError || "").toLowerCase();
  if (
    raw.includes("connection refused") ||
    raw.includes("connect refused") ||
    raw.includes("econnrefused")
  )
    return `${engine.shortName} is not accepting connections.`;
  if (raw.includes("timeout") || raw.includes("timed out"))
    return `${engine.shortName} did not answer in time.`;
  if (/http\s*5\d\d|\b5\d\d\b/.test(raw))
    return `${engine.shortName} is answering with errors.`;
  if (engine.state === "Degraded") return `${engine.shortName} is degraded.`;
  return `${engine.shortName} is unavailable.`;
}

function engineFix(engine: EngineView): string {
  const raw = (engine.lastError || "").toLowerCase();
  if (raw.includes("refused"))
    return "Check the engine address, or start the engine.";
  if (raw.includes("timeout") || raw.includes("timed out"))
    return "Check whether the engine is busy or unreachable.";
  return "Open the engine to see its log and restart it if needed.";
}

function capabilityNames(engines: EngineView[]): string[] {
  return [
    ...new Set(
      engines.flatMap((engine) =>
        engine.label ? engine.label.split(", ") : [],
      ),
    ),
  ];
}

function joinNames(names: string[]): string {
  if (names.length < 3) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** Short headline for the verdict, e.g. "Video is degraded." */
function capabilityHeadline(engines: EngineView[]): string {
  const names = capabilityNames(engines);
  const subject = names.length
    ? joinNames([names[0], ...names.slice(1).map(lowerFirst)])
    : engines.length === 1
      ? engines[0].shortName
      : `${engines.length} engines`;
  const plural =
    names.length > 1 ||
    ["Images", "Decisions", "Embeddings", "Sound effects"].includes(
      names[0] ?? "",
    ) ||
    (!names.length && engines.length > 1);
  const verb = plural ? "are" : "is";
  const degraded = engines.every((engine) => engine.state === "Degraded");
  return `${subject} ${verb} ${degraded ? "degraded" : "unavailable"}.`;
}

type Problem = {
  key: string;
  layer: "Gateway" | "Engine" | "Provider" | "Hardware" | "Access plan";
  title: string;
  headline: string;
  tone: Tone;
  engines?: EngineView[];
  detail?: string;
  fix?: string;
  href?: string | null;
  action?: string;
  disk?: boolean;
  provider?: string;
  refusals?: RequestRecord[];
};

function engineProblems(engines: EngineView[]): Problem[] {
  const grouped = new Map<string, EngineView[]>();
  for (const engine of engines) {
    if (
      !engine.service.enabled ||
      !["Unavailable", "Degraded"].includes(engine.state)
    )
      continue;
    const symptom = engineSymptom(engine).replace(engine.shortName, "");
    grouped.set(symptom, [...(grouped.get(symptom) ?? []), engine]);
  }
  return [...grouped.values()].map((affected) => {
    const single = affected.length === 1;
    return {
      key: `engine:${affected.map((engine) => engine.service.id).join(",")}`,
      layer: "Engine",
      title: single
        ? engineSymptom(affected[0])
        : `${affected.length} engines ${engineSymptom(affected[0])
            .replace(affected[0].shortName, "")
            .trim()
            .replace(/^is /, "are ")
            .replace(/^did /, "did ")}`,
      headline: capabilityHeadline(affected),
      tone: affected.every((engine) => engine.state === "Degraded")
        ? "warn"
        : "bad",
      engines: affected,
      fix: engineFix(affected[0]),
      href: single
        ? `/engines/?service=${encodeURIComponent(affected[0].service.id)}`
        : "/engines/",
      action: single ? `Inspect ${affected[0].shortName}` : "Inspect engines",
    } satisfies Problem;
  });
}

interface RequestRecord {
  receipt?: {
    request_id?: string;
    status?: string;
    backend?: string;
    intent?: string;
    warnings?: string[];
  };
  created_at?: number;
  person_label?: string;
  app_label?: string;
}


/** Spending caps the gateway enforced, read from the warnings on refused receipts. */
function capProblemsFrom(records: RequestRecord[]): Problem[] {
  const now = Date.now() / 1000;
  const refusals = records.filter((record) => {
    if (String(record.receipt?.status ?? "").toLowerCase() !== "rejected")
      return false;
    if (record.created_at && now - record.created_at > 86_400) return false;
    return (record.receipt?.warnings ?? []).some((text) =>
      /allowance|spending|cap\b/i.test(text),
    );
  });
  if (!refusals.length) return [];
  // The newest refusal's own sentence explains all of them.
  const warning =
    (refusals[0].receipt?.warnings ?? []).find((text) =>
      /allowance|spending|cap\b/i.test(text),
    ) ?? "";
  return [
    {
      key: "cap",
      layer: "Access plan",
      title: "Paid requests are being refused.",
      headline: "A spending cap was reached.",
      tone: "warn",
      detail: warning,
      href: "/people?tab=plans",
      action: "Open access plans",
      refusals,
    },
  ];
}

function useRecentFailures(enabled: boolean) {
  return useQuery({
    queryKey: ["status", "recent-requests"],
    enabled,
    queryFn: async (): Promise<RequestRecord[]> => {
      const response = await fetch("/admin/api/activity/requests?limit=50", {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Recent requests could not be loaded.");
      const payload = (await response.json()) as { records?: RequestRecord[] };
      return payload.records ?? [];
    },
    retry: false,
  });
}

function FactRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-4">
      <dt className="text-muted-foreground text-sm">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}

function ProblemCard({
  problem,
  failures,
  onDiskHelp,
  primary,
}: {
  problem: Problem;
  failures: RequestRecord[];
  onDiskHelp: () => void;
  primary: boolean;
}) {
  const engine = problem.engines?.length === 1 ? problem.engines[0] : null;
  const engineIds = new Set(
    (problem.engines ?? []).map((item) => item.service.id),
  );
  const failed = failures.filter(
    (record) =>
      record.receipt?.backend &&
      engineIds.has(record.receipt.backend) &&
      !["succeeded", "success", "completed"].includes(
        String(record.receipt.status ?? "").toLowerCase(),
      ),
  );
  const people = [
    ...new Set(failed.map((record) => record.person_label).filter(Boolean)),
  ] as string[];
  const latest = failed[0];
  const series = engine?.backend?.history?.status_series;
  const percent =
    engine?.backend?.history?.uptime_pct_24h != null
      ? Math.round(engine.backend.history.uptime_pct_24h * 10) / 10
      : uptimePercent(series);
  const trailing = hoursUnhealthy(series);
  const capabilities = capabilityNames(problem.engines ?? []);
  return (
    <article
      className={cn(
        "bg-card rounded-panel border",
        problem.tone === "bad" ? "border-danger/40" : "border-warning/40",
      )}
    >
      <div className="space-y-4 p-5">
        <h3 className="text-lg font-semibold">{problem.title}</h3>
        <dl className="space-y-3">
          <FactRow label="Where">
            {problem.layer === "Hardware"
              ? "The gateway’s computer"
              : problem.layer}
            {problem.engines?.length
              ? ` · ${problem.engines.map((item) => item.shortName).join(", ")}`
              : problem.provider
                ? ` · ${problem.provider}`
                : ""}
          </FactRow>
          {capabilities.length ? (
            <FactRow label="Affects">
              {`${joinNames(capabilities)}, for everyone who uses ${capabilities.length > 1 ? "them" : "it"}`}
            </FactRow>
          ) : null}
          {engine?.lastError ? (
            <FactRow label="Last error">
              <span className="font-mono text-sm">{engine.lastError}</span>
            </FactRow>
          ) : problem.detail ? (
            <FactRow label="Detail">{problem.detail}</FactRow>
          ) : null}
          {series?.length ? (
            <FactRow label="Last 24 hours">
              <span className="flex items-center gap-3">
                <UptimeBar
                  series={series}
                  now={engine?.checkedAt}
                  label={engine?.shortName ?? "Engine"}
                  className="max-w-72 flex-1"
                />
                <span className="figures shrink-0">{percent}% up</span>
              </span>
              {trailing > 1 ? (
                <span className="text-muted-foreground mt-1 block">
                  Not fully up for the last {trailing} hours.
                </span>
              ) : null}
            </FactRow>
          ) : null}
          {problem.refusals?.length ? (
            <FactRow label="Refused">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {[
                  ...new Set(
                    problem.refusals
                      .map((record) => record.person_label)
                      .filter(Boolean),
                  ),
                ]
                  .slice(0, 4)
                  .map((name) => (
                    <PersonMark key={name} name={name!} size="xs" />
                  ))}
                <span>
                  {problem.refusals.length} request
                  {problem.refusals.length === 1 ? "" : "s"}, latest{" "}
                  <RelativeTime
                    ts={problem.refusals[0].created_at ?? null}
                    fallback=""
                  />
                </span>
                {problem.refusals[0].receipt?.request_id ? (
                  <a
                    href={`/r/${encodeURIComponent(problem.refusals[0].receipt.request_id)}`}
                    className="text-link font-medium underline-offset-4 hover:underline"
                  >
                    Open receipt
                  </a>
                ) : null}
              </span>
            </FactRow>
          ) : null}
          {latest ? (
            <FactRow label="Recent failures">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {people.slice(0, 4).map((name) => (
                  <PersonMark key={name} name={name} size="xs" />
                ))}
                <span>
                  {latest.person_label ?? "Someone"}
                  {failed.length > 1 ? ` and ${failed.length - 1} more` : ""},
                  latest <RelativeTime ts={latest.created_at ?? null} fallback="" />
                </span>
                {latest.receipt?.request_id ? (
                  <a
                    href={`/r/${encodeURIComponent(latest.receipt.request_id)}`}
                    className="text-link font-medium underline-offset-4 hover:underline"
                  >
                    Open receipt
                  </a>
                ) : null}
              </span>
            </FactRow>
          ) : null}
        </dl>
        {problem.fix ? (
          <p className="text-sm">{problem.fix}</p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {problem.href ? (
            <Button asChild variant={primary ? "default" : "outline"}>
              <Link href={problem.href}>
                {problem.action ?? "Open"}
                <ChevronRight aria-hidden="true" />
              </Link>
            </Button>
          ) : null}
          {problem.disk ? (
            <Button variant="outline" onClick={onDiskHelp}>
              Free disk space
            </Button>
          ) : null}
        </div>
      </div>
    </article>
  );
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
            <Link href={`/engines?service=${encodeURIComponent(engine.service.id)}`}>
              Configure engine
            </Link>
          </Button>
          <details>
            <summary className="min-h-11 cursor-pointer py-3 text-sm">
              Technical fields
            </summary>
            <pre className="bg-muted max-h-96 overflow-auto rounded-control p-3 text-xs">
              {JSON.stringify(engine.service, null, 2)}
            </pre>
          </details>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function engineStateView(engine: EngineView): StateView {
  if (!engine.service.enabled) return { label: "Turned off", tone: "off" };
  if (!engine.state) return { label: "Not checked yet", tone: "unknown" };
  if (engine.state === "Ready to start")
    return { label: "Ready to start", tone: "ok" };
  if (engine.state === "Standby, stopped")
    return { label: "Stopped, starts on request", tone: "ok" };
  return availabilityState(
    engine.state === "Unavailable"
      ? "unavailable"
      : engine.state.toLowerCase(),
  );
}

function EngineRow({
  engine,
  selected,
}: {
  engine: EngineView;
  selected: boolean;
}) {
  const [open, setOpen] = useState(selected);
  const series = engine.backend?.history?.status_series;
  const percent =
    engine.backend?.history?.uptime_pct_24h != null
      ? Math.round(engine.backend.history.uptime_pct_24h * 10) / 10
      : uptimePercent(series);
  const state = engineStateView(engine);
  return (
    <tr
      className="border-b last:border-0"
      aria-current={selected ? "true" : undefined}
    >
      <th scope="row" className="py-2 pr-4 text-left align-middle font-normal">
        <EngineSheet engine={engine} open={open} onOpenChange={setOpen}>
          <button
            type="button"
            className="block max-w-full min-w-0 truncate py-2 text-left font-medium hover:underline max-sm:min-h-11"
            title={engine.name}
          >
            {engine.shortName}
          </button>
        </EngineSheet>
      </th>
      <td className="text-muted-foreground hidden py-2 pr-4 align-middle text-sm md:table-cell">
        {engine.label || null}
      </td>
      <td className="hidden w-48 py-2 pr-3 align-middle sm:table-cell">
        <UptimeBar
          series={series}
          now={engine.checkedAt}
          enabled={engine.service.enabled}
          label={engine.shortName}
        />
      </td>
      <td className="figures text-muted-foreground hidden w-14 py-2 pr-4 text-right align-middle text-sm sm:table-cell">
        {engine.service.enabled && percent != null ? `${percent}%` : ""}
      </td>
      <td className="py-2 align-middle">
        <StateLabel state={state} />
      </td>
    </tr>
  );
}

function money(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value > 0 && value < 0.01 ? "Less than USD 0.01" : `USD ${value.toFixed(2)}`;
}

function ProviderRow({
  provider,
}: {
  provider: ProviderStatusView;
}) {
  const state: StateView =
    provider.enabled === false
      ? { label: "Turned off", tone: "off" }
      : provider.keyState === "missing"
        ? { label: "Key missing", tone: "bad" }
        : provider.reachable === false
          ? { label: "Can’t be reached", tone: "bad" }
          : provider.reachable === true
            ? { label: "Reachable", tone: "ok" }
            : { label: "Not checked yet", tone: "unknown" };
  const spent = money(provider.monthlySpend);
  const cap = provider.monthlyCap ?? null;
  const share =
    typeof provider.monthlySpend === "number" && typeof cap === "number" && cap > 0
      ? Math.min(1, provider.monthlySpend / cap)
      : null;
  return (
    <tr className="border-b last:border-0">
      <th scope="row" className="py-3 pr-4 text-left align-middle font-medium">
        {provider.name}
      </th>
      <td className="text-muted-foreground hidden py-3 pr-4 align-middle text-sm md:table-cell">
        Provider
      </td>
      <td className="hidden py-3 pr-3 align-middle sm:table-cell" colSpan={2}>
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
                    share >= 1 ? "bg-danger" : share >= 0.8 ? "bg-warning" : "bg-link",
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
          <span className="text-muted-foreground text-sm">Spend not recorded</span>
        )}
      </td>
      <td className="py-3 align-middle">
        <StateLabel state={state} />
        {provider.checkedAt ? (
          <span className="text-muted-foreground block text-xs">
            Checked <RelativeTime ts={provider.checkedAt} fallback="" />
          </span>
        ) : null}
      </td>
    </tr>
  );
}

function CapabilityStrip() {
  const { data, isError } = useQuery({
    queryKey: ["account", "status"],
    queryFn: productApi.status,
    retry: false,
  });
  if (isError || !data?.capabilities.length) return null;
  return (
    <section aria-labelledby="capability-strip-heading">
      <SectionHeading id="capability-strip-heading" title="Capabilities" />
      <ul className="grid grid-cols-2 gap-x-8 gap-y-0.5 sm:grid-cols-3 lg:grid-cols-4">
        {data.capabilities.map((capability) => {
          const meta = capabilityMeta(capability.icon_key ?? capability.capability);
          const Icon = meta.icon;
          const state = availabilityState(capability.status, capability.reason_code);
          return (
            <li
              key={capability.capability}
              className="flex min-h-11 min-w-0 items-center gap-2"
            >
              <Icon
                aria-hidden="true"
                className={cn(
                  "size-4 shrink-0",
                  capability.available ? meta.tone : "text-muted-foreground",
                )}
              />
              <span className="min-w-0 truncate text-sm font-medium">
                {capability.capability_title ?? meta.label}
              </span>
              <StateLabel
                state={state}
                iconOnly={state.tone === "ok"}
                className="shrink-0 text-xs"
              />
            </li>
          );
        })}
      </ul>
    </section>
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
  const providers = useOptionalProviderStatus();
  const observedStatus =
    statusIsError || statusData?.current_health?.observation_available !== true
      ? undefined
      : statusData;
  const engines = useMemo(
    () => enginesFrom(registryData, observedStatus, statusIsError ? undefined : statusData),
    [registryData, observedStatus, statusData, statusIsError],
  );
  const tableEngines = engines.filter(
    (engine) =>
      !["provider", "gateway"].includes(engine.service.kind.toLowerCase()) &&
      !["provider", "gateway"].includes(
        (engine.service.role || "").toLowerCase(),
      ),
  );
  const enabledEngines = tableEngines.filter((engine) => engine.service.enabled);
  const healthyStates = new Set(["Ready", "Ready to start", "Standby, stopped"]);
  const allEnginesHealthy =
    enabledEngines.length > 0 &&
    enabledEngines.every((engine) => healthyStates.has(engine.state));
  const signalProblems: Problem[] = (
    statusIsError ? [] : (statusData?.current_health?.signals ?? [])
  )
    .filter((signal) => signal.tone === "warn" || signal.tone === "bad")
    .flatMap((signal) =>
      signal.reasons.map((reason) => ({
        key: `signal:${signal.source}:${reason}`,
        layer:
          signal.source === "api"
            ? ("Gateway" as const)
            : /host|disk|thermal|temperature/i.test(signal.source)
              ? ("Hardware" as const)
              : ("Gateway" as const),
        // The reason is the symptom; the verdict names the layer.
        title: reason,
        headline:
          signal.source === "api"
            ? "The gateway needs attention."
            : "The gateway’s computer needs attention.",
        tone: (signal.tone === "bad" ? "bad" : "warn") as Tone,
        href: null,
        disk: /disk/i.test(signal.source),
      })),
    );
  const providerProblems: Problem[] =
    providers.state === "ready"
      ? providers.providers.flatMap((provider) =>
          provider.enabled === true &&
          (provider.reachable === false || provider.keyState === "missing")
            ? [
                {
                  key: `provider:${provider.id}`,
                  layer: "Provider" as const,
                  title:
                    provider.keyState === "missing"
                      ? "No provider key is set."
                      : "The last connection check failed.",
                  headline:
                    provider.keyState === "missing"
                      ? `${provider.name} has no key.`
                      : `${provider.name} can’t be reached.`,
                  tone: "bad" as Tone,
                  detail:
                    provider.keyState === "missing"
                      ? "Requests that need this provider fail until a key is added."
                      : "Requests that need this provider fail until it answers again.",
                  href: "/providers",
                  action: `Open ${provider.name}`,
                  provider: provider.name,
                },
              ]
            : [],
        )
      : [];
  const loadProblems: Problem[] = [
    ...(statusIsError
      ? [
          {
            key: "status",
            layer: "Gateway" as const,
            title: "The gateway did not return engine health.",
            headline: "Couldn’t check status.",
            tone: "bad" as Tone,
            detail: "Refresh to try again.",
            href: null,
          },
        ]
      : []),
    ...(registryIsError
      ? [
          {
            key: "registry",
            layer: "Gateway" as const,
            title: "The gateway did not return the engine list.",
            headline: "Couldn’t load your engines.",
            tone: "bad" as Tone,
            detail: errorMessage(registryError),
            href: null,
          },
        ]
      : []),
  ];
  const failures = useRecentFailures(true);
  const capProblems = capProblemsFrom(failures.data ?? []);
  const problems = [
    ...loadProblems,
    ...engineProblems(engines),
    ...providerProblems,
    ...capProblems,
    ...signalProblems,
  ].filter(
    (row, index, rows) =>
      rows.findIndex((candidate) => candidate.key === row.key) === index,
  );
  const health = observedStatus?.current_health;
  const incomplete = Object.values(health?.incomplete ?? {}).some(
    (gaps) => gaps?.length,
  );
  const unchecked = providers.state === "ready"
    ? providers.providers.filter(
        (provider) =>
          provider.enabled === null ||
          (provider.enabled === true &&
            (provider.reachable === null || provider.keyState === "unknown")),
      )
    : [];
  let verdict: { tone: Tone; text: string };
  let note: string | null = null;
  if (registryIsPending || statusIsPending || providers.state === "loading") {
    verdict = { tone: "unknown", text: "Checking…" };
  } else if (statusIsError || registryIsError) {
    verdict = { tone: "bad", text: "Couldn’t check status." };
  } else if (
    statusData?.current_health?.state === "empty" &&
    engines.length === 0 &&
    providers.state !== "error"
  ) {
    verdict = { tone: "off", text: "No engines yet." };
  } else if (!health) {
    verdict = { tone: "unknown", text: "Engine health is unknown." };
    note = "The gateway has not reported a current check.";
  } else if (problems.length === 1) {
    verdict = { tone: problems[0].tone, text: problems[0].headline };
  } else if (problems.length > 1) {
    verdict = {
      tone: problems.some((problem) => problem.tone === "bad") ? "bad" : "warn",
      text: `${problems.length} problems need you.`,
    };
  } else if (providers.state === "error") {
    verdict = { tone: "unknown", text: "Engines work; providers unknown." };
    note = "Provider status couldn’t load.";
  } else if (health.state === "empty") {
    verdict = { tone: "off", text: "No engines yet." };
  } else if (health.state === "warn" || health.state === "bad") {
    verdict = { tone: "warn", text: "Something needs attention." };
    note = health.detail || null;
  } else if (health.state !== "ok" || incomplete || !allEnginesHealthy) {
    verdict = { tone: "unknown", text: "Some engines are unchecked." };
  } else if (unchecked.length) {
    verdict = { tone: "ok", text: "Engines are working." };
    note = `${joinNames(unchecked.map((provider) => provider.name))} ${unchecked.length > 1 ? "haven’t" : "hasn’t"} been checked yet.`;
  } else {
    verdict = { tone: "ok", text: "Everything is working." };
  }
  const checkedAt = statusIsError ? null : (statusData?.generated_at ?? null);
  const tableOpen = showAll || !problems.length;
  const troubled = new Set(
    problems.flatMap((problem) =>
      (problem.engines ?? []).map((engine) => engine.service.id),
    ),
  );
  const others = tableEngines.filter((engine) => !troubled.has(engine.service.id));
  const readyCount = others.filter(
    (engine) => engine.service.enabled && healthyStates.has(engine.state),
  ).length;
  const offCount = others.filter((engine) => !engine.service.enabled).length;
  const restCount = others.length - readyCount - offCount;
  const providerList = providers.state === "ready" ? providers.providers : [];
  const foldedSummary = [
    readyCount
      ? `${readyCount} other engine${readyCount === 1 ? "" : "s"} ready`
      : null,
    offCount ? `${offCount} turned off` : null,
    restCount ? `${restCount} not checked yet` : null,
    providerList.length
      ? `${providerList.length} provider${providerList.length === 1 ? "" : "s"}`
      : null,
  ]
    .filter(Boolean)
    .join(", ");
  const refreshAll = () => {
    void refetchStatus();
    void refetchRegistry();
    void providers.refetch();
  };

  return (
    <div className="flex min-w-0 flex-col gap-10">
      <header className="flex flex-col gap-6">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-muted-foreground text-base font-medium">Status</h1>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={refreshAll}
              disabled={statusIsFetching || registryIsFetching}
            >
              <RefreshCcw aria-hidden="true" />
              <span className="max-sm:sr-only">{statusIsFetching ? "Refreshing…" : "Refresh"}</span>
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-sm" aria-label="Status actions">
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
          </div>
        </div>
        <Verdict
          tone={verdict.tone}
          detail={
            <>
              {checkedAt && !statusIsPending ? (
                <span>
                  Checked <RelativeTime ts={checkedAt} fallback="" />
                </span>
              ) : null}
              {note ? <span>{note}</span> : null}
            </>
          }
        >
          {verdict.text}
        </Verdict>
      </header>

      {statusIsPending && registryIsPending ? (
        <div aria-busy="true" className="space-y-4">
          <Skeleton className="h-40 w-full rounded-panel" />
          <Skeleton className="h-64 w-full rounded-panel" />
        </div>
      ) : (
        <>
          {problems.length ? (
            <section aria-labelledby="attention-heading" className="space-y-3">
              <SectionHeading id="attention-heading" title="Needs attention" />
              {problems.map((problem, index) => (
                <ProblemCard
                  key={problem.key}
                  primary={index === 0}
                  problem={problem}
                  failures={failures.data ?? []}
                  onDiskHelp={() => setDiskHelpOpen(true)}
                />
              ))}
            </section>
          ) : null}

          <CapabilityStrip />

          <section aria-labelledby="layers-heading">
            <SectionHeading
              id="layers-heading"
              title="Engines and providers"
              action={
                <Button variant="ghost" size="sm" asChild>
                  <Link href="/engines">
                    Engines
                    <ChevronRight aria-hidden="true" />
                  </Link>
                </Button>
              }
            />
            {registryIsPending ? (
              <Skeleton className="h-48 w-full rounded-panel" />
            ) : !tableEngines.length && providers.state !== "ready" ? (
              <div className="bg-card flex flex-wrap items-center justify-between gap-3 rounded-panel border border-dashed p-5">
                <p className="text-muted-foreground text-sm">
                  Connect an engine on this computer, or a provider you pay
                  for, to turn on chat, images and more.
                </p>
                <Button asChild size="sm">
                  <Link href="/engines">Add an engine</Link>
                </Button>
              </div>
            ) : tableOpen ? (
              <div className="bg-card rounded-panel border px-4 sm:px-5">
                <table className="w-full table-fixed">
                  <caption className="sr-only">Engine status</caption>
                  <thead className="sr-only">
                    <tr>
                      <th scope="col">Engine</th>
                      <th scope="col">Serves</th>
                      <th scope="col">Last 24 hours</th>
                      <th scope="col">Uptime</th>
                      <th scope="col">State</th>
                    </tr>
                  </thead>
                  <colgroup>
                    <col className="w-auto" />
                    <col className="hidden md:table-column" />
                    <col className="hidden w-48 sm:table-column" />
                    <col className="hidden w-14 sm:table-column" />
                    <col className="w-36" />
                  </colgroup>
                  <tbody>
                    {tableEngines.map((engine) => (
                      <EngineRow
                        key={engine.service.id}
                        engine={engine}
                        selected={engine.service.id === selectedServiceId}
                      />
                    ))}
                    {providers.state === "ready"
                      ? providers.providers.map((provider) => (
                          <ProviderRow key={provider.id} provider={provider} />
                        ))
                      : null}
                  </tbody>
                </table>
                {providers.state === "error" ? (
                  <div role="alert" className="text-destructive border-t py-3 text-sm">
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
                className="bg-card hover:border-input flex min-h-11 w-full items-center justify-between gap-3 rounded-panel border px-5 py-3 text-left text-sm"
              >
                <span>{foldedSummary}</span>
                <span className="text-link inline-flex items-center gap-1 font-medium">
                  Show all
                  <ChevronRight aria-hidden="true" className="size-4" />
                </span>
              </button>
            )}
          </section>
        </>
      )}
      <Sheet open={diskHelpOpen} onOpenChange={setDiskHelpOpen}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>Free disk space</SheetTitle>
            <SheetDescription>Check the largest storage areas first.</SheetDescription>
          </SheetHeader>
          <ul className="list-disc space-y-3 px-8 py-4 text-sm">
            <li>Review old filesystem snapshots and remove only snapshots you no longer need.</li>
            <li>Check unused Docker images, containers, and volumes before removing them.</li>
            <li>Clear regenerable model, build, and application caches that you recognize.</li>
          </ul>
        </SheetContent>
      </Sheet>
    </div>
  );
}
