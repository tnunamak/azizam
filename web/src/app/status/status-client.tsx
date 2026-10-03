"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Cpu,
  HardDrive,
  RefreshCcw,
  Server,
  Zap,
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
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import { RelativeTime } from "@/components/ui/relative-time";
import { useOptionalProviderStatus } from "@/lib/provider-status";

type Tone = "ok" | "warn" | "bad" | "neutral";
type LiveBackend = NonNullable<StatusPayload["backends"]>[number];
type EngineView = {
  service: Service;
  health?: CurrentHealthService;
  backend?: LiveBackend;
  name: string;
  label: string;
  role: string;
  state: string;
  tone: Tone;
  endpoint: string;
  models: string[];
  queue: string;
  lastError: string | null;
};

function asTone(value?: string): Tone {
  return value === "ok" || value === "warn" || value === "bad"
    ? value
    : "neutral";
}
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
function valueText(value: unknown): string {
  if (value == null || value === "") return "Not measured yet";
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

function observedTone(service?: CurrentHealthService): Tone {
  if (!service || !service.enabled) return "neutral";
  if (service.observation === "up") return asTone(service.tone);
  if (service.observation === "down")
    return service.tone === "warn" ? "warn" : "bad";
  if (service.observation === "degraded") return "warn";
  return "neutral";
}

function observationLabel(service?: CurrentHealthService): string {
  if (!service) return "Not measured yet";
  if (!service.enabled || service.observation === "disabled") return "Disabled";
  if (service.observation === "up") return "Ready";
  if (service.observation === "down") return "Unavailable";
  if (service.observation === "degraded") return "Degraded";
  if (service.observation === "standby_stopped") return "Standby, stopped";
  if (service.observation === "unavailable") return "Not measured yet";
  return service.observation.replace(/_/g, " ");
}

function capabilityHumanName(service: Service): string {
  const capabilities = new Set(
    service.capabilities.map((capability) => capability.toLowerCase()),
  );
  let label: string;
  if (capabilities.has("image") && capabilities.has("video")) {
    label = "Image & video";
  } else if (capabilities.has("image") || capabilities.has("images")) {
    label = "Image";
  } else if (capabilities.has("video")) {
    label = "Video";
  } else {
    const labels: Record<string, string> = {
      chat: "Chat",
      completions: "Chat",
      speech: "Speech",
      tts: "Speech",
      transcription: "Transcription",
      stt: "Transcription",
      embeddings: "Embeddings",
      rerank: "Rerank",
    };
    const capability =
      service.capabilities.find((cap) => labels[cap.toLowerCase()]) ??
      service.capabilities[0];
    label = capability
      ? (labels[capability.toLowerCase()] ?? titleize(capability))
      : "Engine";
  }
  const protocol =
    typeof service.config.protocol === "string"
      ? service.config.protocol
      : service.kind || service.role || "service";
  return `${label} (${titleize(protocol)})`;
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
): EngineView[] {
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
      const backend = backends.get(service.id);
      const backendExtras = backend as
        | (LiveBackend & { queue?: unknown; queue_depth?: unknown })
        | undefined;
      return {
        service,
        health: h,
        backend,
        name: service.name || capabilityHumanName(service),
        label: capabilityHumanName(service),
        role: service.role || h?.role || titleize(service.kind || "engine"),
        state: !service.enabled
          ? "Disabled"
          : observationLabel(
              h ?? {
                ...({} as CurrentHealthService),
                enabled: service.enabled,
                observation: service.enabled ? "unavailable" : "disabled",
              },
            ),
        tone: service.enabled ? observedTone(h) : "neutral",
        endpoint: endpoint(service, backend),
        models: serviceModels(service),
        queue: valueText(backendExtras?.queue ?? backendExtras?.queue_depth),
        lastError: h?.last_error ?? backend?.last_error ?? null,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function topAnswer(
  engines: EngineView[],
  statusError: boolean,
  providerIssue: string | null,
  hostWarning: string | null,
): string {
  if (statusError)
    return "Status is unavailable; the engine configuration is shown without live health.";
  if (providerIssue) return providerIssue;
  if (hostWarning) return hostWarning;
  if (!engines.length) return "No engines are configured yet.";
  const unavailable = engines.filter(
    (engine) => engine.service.enabled && engine.health?.observation === "down",
  ).length;
  const degraded = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Degraded",
  ).length;
  const unmeasured = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Not measured yet",
  ).length;
  if (unavailable) {
    const first = engines.find(
      (engine) =>
        engine.service.enabled && engine.health?.observation === "down",
    );
    return `${unavailable} enabled engine${unavailable === 1 ? " needs" : "s need"} owner action${first?.lastError ? `: ${first.lastError}` : "."}`;
  }
  if (degraded)
    return `${degraded} enabled engine${degraded === 1 ? " is" : "s are"} degraded; check last error and queue details below.`;
  if (unmeasured)
    return `${unmeasured} enabled engine${unmeasured === 1 ? " has" : "s have"} no live measurement yet.`;
  if (engines.every((engine) => !engine.service.enabled))
    return "All engines are disabled; enable one on Engines to serve requests.";
  const ready = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Ready",
  ).length;
  return ready === 1
    ? "1 enabled engine needs no action."
    : ready > 0
      ? `${ready} enabled engines need no action.`
      : "Engines are stopped on standby; they start on demand.";
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
  const [advanced, setAdvanced] = useState(false);
  return (
    <article
      className={`border-b py-5 last:border-b-0 ${selected ? "ring-primary rounded-md px-3 ring-2" : ""}`}
      aria-current={selected ? "true" : undefined}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-medium">{engine.label}</h3>
            <StatusBadge tone={engine.tone} label={engine.state} />
          </div>
          <p className="text-muted-foreground text-sm">{engine.name}</p>
          <div className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <span>
              <strong>Role:</strong> {engine.role}
            </span>
            <span className="min-w-0 break-all">
              <strong>Endpoint:</strong> {engine.endpoint}
            </span>
            <span>
              <strong>Queue:</strong> {engine.queue}
            </span>
            <span>
              <strong>Loaded:</strong> Not measured yet
            </span>
            <span className="sm:col-span-2">
              <strong>Served models:</strong>{" "}
              {engine.models.length ? engine.models.join(", ") : "Not listed"}
            </span>
          </div>
          {engine.lastError && (
            <p className="bg-danger/10 text-danger rounded-md px-3 py-2 text-sm break-all">
              {engine.lastError}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setAdvanced((open) => !open)}
          >
            Technical fields
          </Button>
          <Button type="button" variant="outline" size="sm" asChild>
            <Link
              href={`/engines?service=${encodeURIComponent(engine.service.id)}`}
            >
              Configure
            </Link>
          </Button>
        </div>
      </div>
      {advanced && (
        <pre className="bg-muted text-muted-foreground mt-4 max-h-72 overflow-auto rounded-md p-3 text-xs">
          {JSON.stringify(engine.service, null, 2)}
        </pre>
      )}
    </article>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function gb(value: number): string {
  return `${value.toFixed(value >= 10 ? 0 : 1)} GB`;
}

function kbToGb(value: number): number {
  return value / 1024 / 1024;
}

function toneFromProducerState(state: unknown): Tone | null {
  if (state === "critical") return "bad";
  if (state === "warn") return "warn";
  if (state === "ok") return "ok";
  if (state === "unknown") return "neutral";
  return null;
}

function thermalStateKey(kind: string): string | null {
  if (kind === "cpu_max_c") return "cpu_thermal_state";
  if (kind === "dimm_max_c") return "dimm_thermal_state";
  if (kind === "nvme_max_c") return "nvme_thermal_state";
  if (kind === "amdgpu_edge_c") return "amdgpu_thermal_state";
  return null;
}

function thermalTone(
  kind: string,
  temp: number,
  thermals: Record<string, unknown>,
): Tone {
  const producerTone = toneFromProducerState(
    thermals[thermalStateKey(kind) ?? ""],
  );
  if (producerTone) return producerTone;
  if (kind === "amdgpu_edge_c")
    return temp >= 100 ? "bad" : temp >= 90 ? "warn" : "ok";
  const nvme = kind.toLowerCase().includes("nvme");
  if (nvme) return temp >= 80 ? "bad" : temp >= 70 ? "warn" : "ok";
  return temp >= 90 ? "bad" : temp >= 80 ? "warn" : "ok";
}

function currentGpus(
  status: StatusPayload | undefined,
): Array<Record<string, unknown>> {
  const raw = (status as Record<string, unknown> | undefined)?.gpus;
  return Array.isArray(raw)
    ? raw.filter((item): item is Record<string, unknown> =>
        Boolean(asRecord(item)),
      )
    : [];
}

function currentDisk(
  status: StatusPayload | undefined,
): Array<{ name: string; pct: number | null }> {
  const raw = asRecord((status as Record<string, unknown> | undefined)?.disk);
  if (!raw) return [];
  const partitions = Array.isArray(raw.partitions) ? raw.partitions : null;
  if (partitions) {
    return partitions.flatMap((partition) => {
      const record = asRecord(partition);
      if (!record) return [];
      return [
        {
          name: String(record.target ?? record.source ?? "partition"),
          pct: asNumber(record.pct_used),
        },
      ];
    });
  }
  return Object.entries(raw).flatMap(([key, value]) => {
    const record = asRecord(value);
    return record ? [{ name: key, pct: asNumber(record.pct_used) }] : [];
  });
}

function Hardware({
  status,
  warnings: hostWarnings,
}: {
  status: StatusPayload | undefined;
  warnings: Array<{ source: string; reason: string }>;
}) {
  const statusRecord = status as Record<string, unknown> | undefined;
  const budgets = asRecord(statusRecord?.budgets);
  const attribution = asRecord(statusRecord?.ram_attribution);
  const totalKb = asNumber(attribution?.total_kb);
  const availableKb = asNumber(attribution?.available_kb);
  const ramUsedPct =
    totalKb && availableKb != null
      ? Math.max(0, Math.min(100, ((totalKb - availableKb) / totalKb) * 100))
      : null;
  const gpus = currentGpus(status);
  const disk = currentDisk(status);
  const thermals =
    asRecord(statusRecord?.thermals) ?? asRecord(statusRecord?.thermal) ?? {};

  return (
    <Section title="Hardware">
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-3">
          <div className="flex items-center gap-2 font-medium">
            <Cpu className="size-4" /> GPU VRAM
          </div>
          {gpus.length ? (
            gpus.map((gpu, index) => {
              const gpuIndex = asNumber(gpu.index) ?? index;
              const usedMb = asNumber(gpu.memory_used_mb);
              const totalMb =
                asNumber(gpu.memory_total_mb) ?? asNumber(gpu.total_memory_mb);
              const value =
                usedMb != null && totalMb
                  ? Math.max(0, Math.min(100, (usedMb / totalMb) * 100))
                  : null;
              const budget = asRecord(budgets?.[`gpu${gpuIndex}`]);
              const totalGb = asNumber(budget?.total_gb);
              const headroomGb = asNumber(budget?.headroom_gb);
              const allocatable =
                totalGb != null && headroomGb != null
                  ? totalGb - headroomGb
                  : null;
              const tone =
                value == null
                  ? "neutral"
                  : value >= 95
                    ? "bad"
                    : value >= 85
                      ? "warn"
                      : "ok";
              return (
                <div key={gpuIndex} className="space-y-1">
                  <div className="flex justify-between text-sm">
                    <span>GPU {gpuIndex}</span>
                    <span>
                      {value != null && usedMb != null && totalMb
                        ? `${(usedMb / 1024).toFixed(1)} / ${(totalMb / 1024).toFixed(1)} GB`
                        : "Not measured yet"}
                    </span>
                  </div>
                  {value != null && (
                    <div className="relative">
                      <Progress
                        value={value}
                        aria-label={`GPU ${gpuIndex} VRAM usage`}
                      />
                      {allocatable != null &&
                        totalMb != null &&
                        totalMb > 0 && (
                          <span
                            role="img"
                            aria-label={`GPU ${gpuIndex} budget line`}
                            className="border-foreground absolute -top-1 h-4 border-l-2"
                            style={{
                              left: `${Math.max(0, Math.min(100, ((allocatable * 1024) / totalMb) * 100))}%`,
                            }}
                          />
                        )}
                    </div>
                  )}
                  {allocatable != null && totalGb != null && (
                    <div className="space-y-1">
                      <p className="text-muted-foreground text-xs">
                        Budget: {gb(allocatable)} allocatable from {gb(totalGb)}{" "}
                        total.
                      </p>
                    </div>
                  )}
                  <p className="text-muted-foreground text-xs">
                    {tone === "bad"
                      ? "Over 95% can fail new requests. Stop work or lower admission."
                      : tone === "warn"
                        ? "Over 85% leaves little room for swaps."
                        : tone === "ok"
                          ? "Below the warning threshold."
                          : "Current VRAM is not measured yet."}
                  </p>
                </div>
              );
            })
          ) : (
            <p className="text-muted-foreground text-sm">
              GPU VRAM is not measured yet.
            </p>
          )}
          <p className="text-muted-foreground text-xs">
            Open Technical details for available GPU usage by service.
          </p>
        </div>
        <div className="space-y-3">
          <div className="flex items-center gap-2 font-medium">
            <Zap className="size-4" /> RAM
          </div>
          {ramUsedPct != null && totalKb != null && availableKb != null ? (
            <>
              <div className="flex justify-between text-sm">
                <span>System memory</span>
                <span>{ramUsedPct.toFixed(0)}% used</span>
              </div>
              <Progress value={ramUsedPct} aria-label="RAM usage" />
              <p className="text-muted-foreground text-xs">
                Used {gb(kbToGb(totalKb - availableKb))} of{" "}
                {gb(kbToGb(totalKb))}. Plain threshold: sustained high memory
                pressure means lower concurrent jobs.
              </p>
            </>
          ) : (
            <p className="text-muted-foreground text-sm">
              RAM usage is not measured yet.
            </p>
          )}
        </div>
        <div className="space-y-3">
          <div className="flex items-center gap-2 font-medium">
            <HardDrive className="size-4" /> Disk
          </div>
          {disk.length ? (
            disk.map(({ name, pct }) => {
              return (
                <div key={name} className="space-y-1">
                  <div className="flex justify-between text-sm">
                    <span>{name}</span>
                    <span>
                      {pct != null
                        ? `${pct.toFixed(0)}% used`
                        : "Not measured yet"}
                    </span>
                  </div>
                  {pct != null && (
                    <Progress
                      value={Math.max(0, Math.min(100, pct))}
                      aria-label={`${name} disk usage`}
                    />
                  )}
                  <p className="text-muted-foreground text-xs">
                    {pct == null
                      ? "Current disk usage is not measured yet."
                      : pct > 90
                        ? "Above 90% full risks failed writes; clear cache or move media."
                        : "Plain threshold: above 90% full risks failed writes."}
                  </p>
                </div>
              );
            })
          ) : (
            <p className="text-muted-foreground text-sm">
              Disk usage is not measured yet.
            </p>
          )}
          {Object.entries(thermals).map(([name, raw]) => {
            const temp = asNumber(raw);
            if (temp == null || !name.endsWith("_c")) return null;
            const tone = thermalTone(name, temp, thermals);
            return (
              <p
                key={name}
                className={`text-xs ${tone === "bad" ? "text-danger" : tone === "warn" ? "text-warning" : "text-muted-foreground"}`}
              >
                {name}: {temp} °C.{" "}
                {tone === "bad"
                  ? "Stop heavy work and check cooling."
                  : tone === "warn"
                    ? "Watch cooling before long jobs."
                    : tone === "ok"
                      ? "Below warning threshold."
                      : "Thermal state is not measured yet."}
              </p>
            );
          })}
        </div>
      </div>
      <div className="mt-6 space-y-3">
        <h3 className="font-medium">Host warnings</h3>
        {hostWarnings.length ? (
          <div className="space-y-2">
            {hostWarnings.map((warning, index) => (
              <div
                key={index}
                className="flex gap-2 rounded-md border p-3 text-sm"
              >
                <AlertTriangle className="text-warning mt-0.5 size-4" />
                <span>
                  <strong>{warning.source}:</strong> {warning.reason}. Suggested
                  action: inspect the host before starting more work.
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">
            {status
              ? "No host warning is reported by the status API."
              : "Host warnings are not measured yet."}
          </p>
        )}
      </div>
    </Section>
  );
}

export function StatusClient() {
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const [showDisabled, setShowDisabled] = useState(false);
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
  const providers = useOptionalProviderStatus();
  const observedStatus =
    statusIsError || statusData?.current_health?.observation_available === false
      ? undefined
      : statusData;
  const engines = useMemo(
    () => enginesFrom(registryData, observedStatus),
    [registryData, observedStatus],
  );
  const visibleEngines = engines.filter(
    (engine) =>
      showDisabled ||
      engine.service.enabled ||
      engine.service.id === selectedServiceId,
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
  const apiSignal = statusData?.current_health?.signals?.find(
    (signal) => signal.source === "api",
  );
  const hostWarning = apiSignal?.reasons[0]
    ? `The gateway needs attention: ${apiSignal.reasons[0]}.`
    : firstHostWarning
      ? `${firstHostWarning.source} needs owner action: ${firstHostWarning.reason}.`
      : null;
  const answer = registryIsError
    ? "Engine configuration is unavailable; retry to check the configured engines."
    : registryIsPending || statusIsPending
      ? "Checking engine status…"
      : providers.state === "error"
        ? "Provider status is unavailable; retry to check remote connections."
        : topAnswer(engines, statusIsError, providerIssue, hostWarning);
  const refreshAll = () => {
    void refetchStatus();
    void refetchRegistry();
    void providers.refetch();
  };

  if (statusIsPending && registryIsPending)
    return (
      <div className="mx-auto flex max-w-6xl flex-col gap-6" aria-busy="true">
        <h1 className="text-3xl font-semibold tracking-tight">Status</h1>
        <div className="bg-card text-muted-foreground rounded-lg border p-4">
          Loading engine status…
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
          <Button
            type="button"
            variant="outline"
            onClick={refreshAll}
            disabled={statusIsFetching || registryIsFetching}
          >
            <RefreshCcw className="size-4" /> Refresh
          </Button>
        </div>
        <p className="text-lg font-medium">{answer}</p>
        {statusData?.generated_at != null && (
          <p className="text-muted-foreground text-sm">
            {statusIsError ? "Last successful observation: " : "Observed: "}
            <RelativeTime ts={statusData.generated_at} />
          </p>
        )}
        {(statusIsError || registryIsError) && (
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-md border p-3 text-sm"
          >
            {statusIsError
              ? errorMessage(statusError)
              : errorMessage(registryError)}
          </div>
        )}
      </header>
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
            <Button type="button" asChild>
              <Link href="/engines">Open engines</Link>
            </Button>
          </div>
        }
      >
        {registryIsPending ? (
          <div className="text-muted-foreground text-sm">Loading engines…</div>
        ) : visibleEngines.length ? (
          <div className="space-y-3">
            {visibleEngines.map((engine) => (
              <EngineRow
                key={engine.service.id}
                engine={engine}
                selected={engine.service.id === selectedServiceId}
              />
            ))}
          </div>
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
                            : "Not measured yet"
                      }
                    />
                  </div>
                  <p className="text-muted-foreground mt-2 text-sm">
                    Enabled:{" "}
                    {provider.enabled === true
                      ? "yes"
                      : provider.enabled === false
                        ? "no"
                        : "unknown"}
                  </p>
                  <p className="text-muted-foreground text-sm">
                    Key: {provider.keyState}
                  </p>
                  <p className="text-muted-foreground text-sm">
                    Spend: {provider.spendText}
                  </p>
                </article>
              ))}
            </div>
          ) : (
            <p className="text-muted-foreground text-sm">
              No providers are reported by the provider status API.
            </p>
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
      <Hardware
        status={statusIsError ? undefined : statusData}
        warnings={hostWarnings}
      />
      <Section title="Technical details">
        <details className="group">
          <summary className="flex min-h-11 cursor-pointer items-center gap-2 text-sm font-medium">
            <Server className="size-4" /> Raw probe data
          </summary>
          <pre className="bg-muted text-muted-foreground mt-3 max-h-[520px] overflow-auto rounded-md p-3 text-xs">
            {JSON.stringify(
              { status: statusData ?? null, providers: providers.raw ?? null },
              null,
              2,
            )}
          </pre>
        </details>
      </Section>
    </div>
  );
}
