"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  MoreHorizontal,
  RefreshCcw,
  Server,
} from "lucide-react";

import {
  api,
  ApiError,
  queryKeys,
  type CurrentHealthService,
  type Service,
  type ServiceRegistry,
  type StatusPayload,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
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
import { useOptionalProviderStatus } from "@/lib/provider-status";

import {
  engineDisplayName,
  engineHealthPresentation,
} from "@/lib/health-presentation";

type Tone = "ok" | "warn" | "bad" | "neutral";
type LiveBackend = NonNullable<StatusPayload["backends"]>[number];
type EngineView = {
  service: Service;
  health?: CurrentHealthService;
  backend?: LiveBackend;
  name: string;
  label: string;
  group: string;
  role: string;
  state: string;
  tone: Tone;
  endpoint: string;
  models: string[];
  queue: string;
  lastError: string | null;
};

function toneClass(tone: Tone): string {
  return tone === "ok"
    ? "border border-success/40 bg-success/10 text-success"
    : tone === "warn"
      ? "border border-warning/40 bg-warning/10 text-warning"
      : tone === "bad"
        ? "border border-danger/40 bg-danger/10 text-danger"
        : "border border-border bg-muted text-muted-foreground";
}
function dotClass(tone: Tone): string {
  return tone === "ok"
    ? "bg-success"
    : tone === "warn"
      ? "bg-warning"
      : tone === "bad"
        ? "bg-danger"
        : "bg-muted-foreground";
}
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
    (status?.backends ?? []).flatMap((backend) => {
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
        backend?.live_status ?? backend?.status,
      );
      return {
        service,
        health: h,
        backend,
        name: engineDisplayName(service, display),
        label:
          display?.capability_labels?.filter((label) => !/\((?:speech|transcription)\)/i.test(label)).join(", ") || "",
        group: display?.capability_title || "Engines",
        role: service.role || h?.role || titleize(service.kind || "engine"),
        state: healthPresentation.label,
        tone: healthPresentation.tone,
        endpoint: endpoint(service, backend),
        models: serviceModels(service),
        queue: valueText(backendExtras?.queue ?? backendExtras?.queue_depth),
        lastError: h?.last_error ?? backend?.last_error ?? null,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function plainEngineProblem(
  engine: EngineView,
  state: "Unavailable" | "Degraded" = "Unavailable",
): string {
  const labels =
    engine.label
      ? engine.label.split(", ")
      : [];
  const source = labels.length
    ? labels.length === 1
      ? labels[0]
      : `${labels[0]} and ${labels.slice(1).map(lowerFirst).join(" and ")}`
    : engine.name || "An engine";
  const plural =
    labels.length > 1 ||
    ["Images", "Decisions", "Embeddings", "Sound effects"].includes(labels[0]);
  const verb = plural ? "are" : "is";
  const raw = (engine.lastError || "").toLowerCase();
  if (state === "Degraded")
    return `${source} ${verb} degraded.`;
  if (
    raw.includes("connection refused") ||
    raw.includes("connect refused") ||
    raw.includes("econnrefused")
  ) {
    return `${source} ${verb} not accepting connections. Check the engine address or start the engine.`;
  }
  if (raw.includes("timeout") || raw.includes("timed out")) {
    return `${source} did not answer in time. Check whether the engine is busy or unreachable.`;
  }
  return `${source} ${verb} unavailable.`;
}

function engineProblemRows(engines: EngineView[]) {
  const grouped = new Map<string, EngineView[]>();
  for (const engine of engines) {
    if (!engine.service.enabled || !["Unavailable", "Degraded"].includes(engine.state))
      continue;
    const message = plainEngineProblem(
      engine,
      engine.state as "Unavailable" | "Degraded",
    );
    grouped.set(message, [...(grouped.get(message) ?? []), engine]);
  }
  return [...grouped.entries()].map(([message, affected]) => ({
    key: `engine:${message}`,
    title: message,
    detail: `${affected.length} engine${affected.length === 1 ? "" : "s"} affected.`,
    href: "/engines",
  }));
}

function StatusBadge({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 text-xs font-medium ${toneClass(tone)}`}
    >
      <span className={`size-2 rounded-full ${dotClass(tone)}`} />
      {label}
    </span>
  );
}
function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-4 border-t pt-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
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
  return (
    <tr
      className="border-b last:border-0"
      aria-current={selected ? "true" : undefined}
    >
      <th scope="row" className="py-3 pr-3 text-left align-top font-medium">
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetTrigger asChild>
            <Button
              variant="ghost"
              className="h-auto min-h-11 w-full max-w-full min-w-0 justify-start overflow-hidden px-0 text-left"
              title={engine.name}
            >
              <span className="block min-w-0 truncate">{engine.name}</span>
            </Button>
          </SheetTrigger>
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
                <pre className="bg-muted max-h-96 overflow-auto rounded-md p-3 text-xs">
                  {JSON.stringify(engine.service, null, 2)}
                </pre>
              </details>
            </div>
          </SheetContent>
        </Sheet>
      </th>
      <td className="py-3 pr-3 align-middle">
        {engine.state && engine.state !== "Unknown" && (
          <StatusBadge tone={engine.tone} label={engine.state} />
        )}
      </td>
      <td className="hidden py-3 pr-3 align-middle text-sm break-words sm:table-cell">
        {engine.label || null}
      </td>
    </tr>
  );
}

export function StatusClient() {
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const [showDisabled, setShowDisabled] = useState(false);
  const [diskHelpOpen, setDiskHelpOpen] = useState(false);
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
    () => enginesFrom(registryData, observedStatus, statusData),
    [registryData, observedStatus, statusData],
  );
  const visibleEngines = engines.filter(
    (engine) =>
      !["provider", "gateway"].includes(engine.service.kind.toLowerCase()) &&
      !["provider", "gateway"].includes((engine.service.role || "").toLowerCase()) &&
      (showDisabled ||
        engine.service.enabled ||
        engine.service.id === selectedServiceId),
  );
  const hostWarnings = (
    statusIsError ? [] : (statusData?.current_health?.signals ?? [])
  )
    .filter((signal) => /host|disk|thermal|temperature/i.test(signal.source))
    .flatMap((signal) =>
      signal.reasons.map((reason) => ({ source: signal.source, reason })),
    );
  const providerFailure =
    providers.state === "ready"
      ? providers.providers.find(
          (provider) =>
            provider.enabled === true &&
            (provider.reachable === false || provider.keyState === "missing"),
        )
      : null;
  const providerIssue = providerFailure
    ? `${providerFailure.name} needs owner action: ${providerFailure.reachable === false ? "provider is unreachable" : "provider key is missing"}.`
    : null;
  const firstHostWarning = hostWarnings[0];
  const apiSignal = observedStatus?.current_health?.signals?.find(
    (signal) => signal.source === "api",
  );
  const hostWarning = apiSignal?.reasons[0]
    ? apiSignal.reasons[0]
    : firstHostWarning
      ? firstHostWarning.reason
      : null;
  const refreshAll = () => {
    void refetchStatus();
    void refetchRegistry();
    void providers.refetch();
  };
  const enabledEngines = visibleEngines.filter(
    (engine) => engine.service.enabled,
  );
  const enabledEngineCount = enabledEngines.length;
  const healthyEngineStates = new Set(["Ready", "Ready to start"]);
  const allVisibleEnginesHealthy =
    enabledEngineCount > 0 &&
    enabledEngines.every((engine) => healthyEngineStates.has(engine.state));
  const actionRows = [
    statusIsError
      ? {
          key: "status",
          title: "Could not check engine status",
          detail: "Refresh to try again.",
          href: null,
        }
      : null,
    registryIsError
      ? {
          key: "registry",
          title: "Engine list could not load",
          detail: errorMessage(registryError),
          href: null,
        }
      : null,
    ...engineProblemRows(engines),
    providerIssue
      ? {
          key: "provider",
          title: "Provider needs owner action",
          detail: providerIssue,
          href: "/providers",
        }
      : null,
    hostWarning
      ? {
          key: "hardware",
          title: "Hardware needs attention",
          detail: hostWarning,
          href: null,
        }
      : null,
  ].filter(
    (
      row,
    ): row is {
      key: string;
      title: string;
      detail: string;
      href: string | null;
    } => row != null,
  );
  const health = observedStatus?.current_health;
  const incomplete = Object.values(health?.incomplete ?? {}).some(
    (gaps) => gaps?.length,
  );
  const providersIncomplete =
    providers.state === "ready" &&
    providers.providers.some(
      (provider) =>
        provider.enabled === null ||
        (provider.enabled &&
          (provider.reachable === null || provider.keyState === "unknown")),
    );
  let answer: string;
  if (registryIsPending || statusIsPending || providers.state === "loading") {
    answer = "Checking system status...";
  } else if (statusIsError || registryIsError) {
    answer = "System status unavailable";
  } else if (
    statusData?.current_health?.state === "empty" &&
    engines.length === 0 &&
    providers.state !== "error"
  ) {
    answer = "No engines configured";
  } else if (!health) {
    answer = "System status unknown";
  } else if (providers.state === "error" || incomplete) {
    answer = "System status partially checked";
  } else if (health.state === "empty") {
    answer = "No engines configured";
  } else if (actionRows.length) {
    answer = actionRows.length === 1
      ? "1 problem needs you"
      : `${actionRows.length} problems need you`;
  } else if (health.state === "warn" || health.state === "bad") {
    answer = "System needs attention";
  } else if (health.state !== "ok" || !allVisibleEnginesHealthy || providersIncomplete) {
    answer = "System status partially checked";
  } else {
    answer = "All systems normal";
  }
  const startingOnRequest = visibleEngines.filter(
    (engine) => engine.service.enabled && engine.state === "Ready to start",
  ).length;
  const engineSummary = allVisibleEnginesHealthy
    ? `${visibleEngines.length} engine${visibleEngines.length === 1 ? "" : "s"}${startingOnRequest ? ` · ${startingOnRequest} on standby` : ""} · Show`
    : `${visibleEngines.length} engine${visibleEngines.length === 1 ? "" : "s"}`;

  if (statusIsPending && registryIsPending)
    return (
      <div className="mx-auto flex max-w-6xl flex-col gap-6" aria-busy="true">
        <h1 className="text-3xl font-semibold tracking-tight">Status</h1>
        <div className="bg-card text-muted-foreground rounded-lg border p-4">
          <p role="status" aria-live="polite" aria-atomic="true">{answer}</p>
        </div>
        <div className="bg-card h-40 rounded-lg border" />
      </div>
    );

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6">
      <header className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight">Status</h1>
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="icon" aria-label="Status actions">
                <MoreHorizontal className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem asChild>
                <a href="/status/raw">
                  <Server className="size-4" /> Technical details
                </a>
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={(event) => {
                  event.preventDefault();
                  refreshAll();
                }}
                disabled={statusIsFetching || registryIsFetching}
              >
                <RefreshCcw className="size-4" /> Refresh
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <p role="status" aria-live="polite" aria-atomic="true" className="text-lg font-medium">{answer}</p>
      </header>
      <Section title={actionRows.length ? "Needs attention" : "Summary"}>
        {actionRows.length ? (
          <div className="divide-y rounded-xl border">
            {actionRows.map((row) => (
              <div
                key={row.key}
                className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="font-medium">{row.title}</p>
                  <p className="text-muted-foreground text-sm">{row.detail}</p>
                </div>
                {row.href && (
                  <Button variant="outline" size="sm" asChild>
                    <Link href={row.href}>Open</Link>
                  </Button>
                )}
                {row.key === "hardware" && hostWarnings.some(({ source }) => /disk/i.test(source)) && (
                  <Button variant="outline" size="sm" onClick={() => setDiskHelpOpen(true)}>
                    Free disk space
                  </Button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">
            {statusData?.current_health?.state === "empty"
              ? "Add an engine to start routing."
              : answer === "All systems normal"
                ? "No owner action is needed from the latest observation."
                : "A complete current check is needed to confirm system health."}
          </p>
        )}
      </Section>
      <Section
        title="Engines"
        action={
          <div className="flex flex-wrap gap-2">
            <label className="flex min-h-11 items-center gap-2 text-sm">
              <Switch
                checked={showDisabled}
                onCheckedChange={setShowDisabled}
              />{" "}
              Show disabled
            </label>
            <Button type="button" variant="outline" asChild>
              <Link href="/engines">Open engines</Link>
            </Button>
          </div>
        }
      >
        {registryIsPending ? (
          <div className="text-muted-foreground text-sm">Loading engines…</div>
        ) : visibleEngines.length ? (
          <details open={!allVisibleEnginesHealthy} className="space-y-4">
            <summary className="min-h-11 cursor-pointer text-sm font-medium">
              {engineSummary}
            </summary>
            <table className="w-full table-fixed text-sm">
              <caption className="sr-only">Engine status</caption>
              <thead>
                <tr className="text-muted-foreground border-b text-left text-xs">
                  <th className="w-3/5 py-2 font-medium sm:w-1/3">Engine</th>
                  <th className="py-2 font-medium sm:w-1/5">State</th>
                  <th className="hidden py-2 font-medium sm:table-cell">
                    Serves
                  </th>
                </tr>
              </thead>
              <tbody>
                {visibleEngines.map((engine) => (
                  <EngineRow
                    key={engine.service.id}
                    engine={engine}
                    selected={engine.service.id === selectedServiceId}
                  />
                ))}
              </tbody>
            </table>
          </details>
        ) : (
          <p className="text-muted-foreground text-sm">
            No enabled engines. Turn on Show disabled or add an engine from
            Engines.
          </p>
        )}
      </Section>
      {providers.state === "loading" && (
        <Section title="Providers">
          <p className="text-muted-foreground text-sm">
            Checking provider status…
          </p>
        </Section>
      )}
      {providers.state === "ready" && (
        <Section title="Providers">
          {providers.providers.length > 0 ? (
            <div className="grid gap-3 lg:grid-cols-3">
              {providers.providers.map((provider) => (
                <article key={provider.id} className="border-b py-4">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="font-medium">{provider.name}</h3>
                    <StatusBadge
                      tone={provider.tone}
                      label={
                        provider.reachable === false
                          ? "Unreachable"
                          : provider.reachable === true
                            ? "Reachable"
                            : "Configured"
                      }
                    />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {provider.enabled !== null && provider.enabled !== undefined && (
                      <StatusBadge
                        tone={provider.enabled ? "ok" : "neutral"}
                        label={provider.enabled ? "Enabled" : "Disabled"}
                      />
                    )}
                    {provider.keyState !== "unknown" && (
                      <StatusBadge
                        tone={provider.keyState === "set" ? "ok" : "warn"}
                        label={provider.keyState === "set"
                          ? provider.keySuffix
                            ? `Key ends in ${provider.keySuffix}`
                            : "Key set"
                          : "Key missing"}
                      />
                    )}
                  </div>
                  {!/not measured|unknown/i.test(provider.spendText) && (
                    <p className="text-muted-foreground text-sm">
                      Spend: {provider.spendText}
                    </p>
                  )}
                </article>
              ))}
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-muted-foreground text-sm">
                No remote providers are configured.
              </p>
              <Button type="button" variant="outline" size="sm" asChild>
                <Link href="/engines">Review connections</Link>
              </Button>
            </div>
          )}
        </Section>
      )}
      {providers.state === "error" && (
        <Section title="Providers">
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-md border p-3 text-sm"
          >
            Provider status API exists but failed: {providers.message}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={providers.refetch}
          >
            Retry providers
          </Button>
        </Section>
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
