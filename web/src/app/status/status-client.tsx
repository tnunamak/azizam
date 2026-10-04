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
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetTrigger,
} from "@/components/ui/sheet";
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
  if (!service) return "Unknown";
  if (!service.enabled || service.observation === "disabled") return "Disabled";
  if (service.observation === "up")
    return service.tone === "bad"
      ? "Unavailable"
      : service.tone === "warn"
        ? "Degraded"
        : "Ready";
  if (service.observation === "down") return "Unavailable";
  if (service.observation === "degraded") return "Degraded";
  if (service.observation === "idle") return "Ready to start";
  if (service.observation === "busy") return "Busy";
  if (service.observation === "starting") return "Starting";
  if (service.observation === "standby_stopped") return "Standby, stopped";
  if (service.observation === "unavailable") return "Unknown";
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
  return label;
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
    (engine) => engine.service.enabled && engine.state === "Unavailable",
  ).length;
  const degraded = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Degraded",
  ).length;
  const unmeasured = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Unknown",
  ).length;
  if (unavailable) {
    const first = engines.find(
      (engine) => engine.service.enabled && engine.state === "Unavailable",
    );
    return `${unavailable} enabled engine${unavailable === 1 ? " needs" : "s need"} owner action${first?.lastError ? `: ${first.lastError}` : "."}`;
  }
  if (degraded)
    return `${degraded} enabled engine${degraded === 1 ? " is" : "s are"} degraded; check last error and queue details below.`;
  if (unmeasured)
    return `${unmeasured} enabled engine${unmeasured === 1 ? " has" : "s have"} no live measurement yet.`;
  if (engines.every((engine) => !engine.service.enabled))
    return "All engines are disabled; enable one on Engines to serve requests.";
  const busy = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Busy",
  ).length;
  const starting = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Starting",
  ).length;
  if (busy || starting)
    return (
      [
        busy ? `${busy} engine${busy === 1 ? " is" : "s are"} busy` : "",
        starting
          ? `${starting} engine${starting === 1 ? " is" : "s are"} starting`
          : "",
      ]
        .filter(Boolean)
        .join("; ") + "."
    );
  const ready = engines.filter(
    (engine) => engine.service.enabled && engine.state === "Ready",
  ).length;
  return ready === 1
    ? "1 enabled engine needs no action."
    : ready > 0
      ? `${ready} enabled engines need no action.`
      : "No engine is currently serving requests. Open an engine for its observed state.";
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
                {engine.label} · {engine.state}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-5 px-4 pb-6 font-normal">
              <dl className="space-y-3 text-sm">
                {[
                  ["Role", engine.role],
                  ["Endpoint", engine.endpoint],
                  ["Queue", engine.queue],
                  ["Served models", engine.models.join(", ") || "Not listed"],
                  ["Last error", engine.lastError || "No error reported"],
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
      <td className="py-3 pr-3 align-top">
        <StatusBadge tone={engine.tone} label={engine.state} />
      </td>
      <td className="hidden py-3 pr-3 align-top text-sm break-words sm:table-cell">
        {engine.models.join(", ") || engine.label}
      </td>
      <td className="hidden py-3 align-top text-sm break-words md:table-cell">
        {engine.lastError ?? "—"}
      </td>
    </tr>
  );
}

function engineCapability(service: Service): string {
  const labels: Record<string, string> = {
    chat: "Chat",
    completions: "Chat",
    image: "Images & video",
    images: "Images & video",
    video: "Images & video",
    speech: "Speech",
    tts: "Speech",
    audio: "Audio",
    music: "Music",
    sfx: "Sound effects",
    transcribe: "Transcription",
    transcription: "Transcription",
    stt: "Transcription",
    decide: "Decisions",
    decision: "Decisions",
    embed: "Embeddings",
    embeddings: "Embeddings",
    rerank: "Rerank",
  };
  return labels[service.capabilities[0]?.toLowerCase()] ?? "Other engines";
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
  warnings,
}: {
  status: StatusPayload | undefined;
  warnings: Array<{ source: string; reason: string }>;
}) {
  const record = status as Record<string, unknown> | undefined;
  const budgets = asRecord(record?.budgets);
  const ram = asRecord(record?.ram_attribution);
  const totalKb = asNumber(ram?.total_kb);
  const availableKb = asNumber(ram?.available_kb);
  const gpus = currentGpus(status).filter(
    (gpu) =>
      asNumber(gpu.memory_used_mb) != null &&
      (asNumber(gpu.memory_total_mb) ?? asNumber(gpu.total_memory_mb)) != null,
  );
  const disk = currentDisk(status).filter((item) => item.pct != null);
  const thermals =
    asRecord(record?.thermals) ?? asRecord(record?.thermal) ?? {};
  const hasThermals = Object.entries(thermals).some(
    ([name, value]) => name.endsWith("_c") && asNumber(value) != null,
  );
  if (
    !gpus.length &&
    !totalKb &&
    !disk.length &&
    !warnings.length &&
    !hasThermals
  )
    return null;
  return (
    <Section title="Hardware">
      <div className="grid gap-6 lg:grid-cols-3">
        {gpus.length > 0 && (
          <div className="space-y-3">
            <h3 className="flex items-center gap-2 text-sm font-medium">
              <Cpu className="size-4" />
              GPU VRAM
            </h3>
            {gpus.map((gpu, index) => {
              const id = asNumber(gpu.index) ?? index;
              const used = asNumber(gpu.memory_used_mb)!;
              const total = (asNumber(gpu.memory_total_mb) ??
                asNumber(gpu.total_memory_mb))!;
              const budget = asRecord(budgets?.[`gpu${id}`]);
              const capacity = asNumber(budget?.total_gb);
              const headroom = asNumber(budget?.headroom_gb);
              return (
                <div key={id} className="space-y-2">
                  <p className="flex justify-between gap-2 text-sm">
                    <span>GPU {id}</span>
                    <span className="tabular-nums">
                      {(used / 1024).toFixed(1)} / {(total / 1024).toFixed(1)}{" "}
                      GB
                    </span>
                  </p>
                  {total > 0 && (
                    <div className="relative">
                      <Progress
                        value={Math.min(100, (used / total) * 100)}
                        aria-label={`GPU ${id} VRAM usage`}
                      />
                      {capacity != null && headroom != null && (
                        <span
                          role="img"
                          aria-label={`GPU ${id} budget line`}
                          className="border-foreground absolute -top-1 h-4 border-l-2"
                          style={{
                            left: `${Math.max(0, Math.min(100, (((capacity - headroom) * 1024) / total) * 100))}%`,
                          }}
                        />
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {totalKb != null && totalKb > 0 && availableKb != null && (
          <div className="space-y-3">
            <h3 className="flex items-center gap-2 text-sm font-medium">
              <Zap className="size-4" />
              RAM
            </h3>
            <p className="text-sm tabular-nums">
              {gb(kbToGb(totalKb - availableKb))} / {gb(kbToGb(totalKb))} used
            </p>
            <Progress
              value={Math.max(
                0,
                Math.min(100, ((totalKb - availableKb) / totalKb) * 100),
              )}
              aria-label="RAM usage"
            />
          </div>
        )}
        {disk.length > 0 && (
          <div className="space-y-3">
            <h3 className="flex items-center gap-2 text-sm font-medium">
              <HardDrive className="size-4" />
              Disk
            </h3>
            {disk.map(({ name, pct }) => (
              <div key={name} className="space-y-2">
                <p className="flex justify-between gap-2 text-sm">
                  <span className="break-all">{name}</span>
                  <span className="shrink-0 tabular-nums">
                    {pct!.toFixed(0)}% used
                  </span>
                </p>
                <Progress
                  value={Math.max(0, Math.min(100, pct!))}
                  aria-label={`${name} disk usage`}
                />
              </div>
            ))}
          </div>
        )}
      </div>
      {Object.entries(thermals).map(([name, raw]) => {
        const temp = asNumber(raw);
        if (temp == null || !name.endsWith("_c")) return null;
        const tone = thermalTone(name, temp, thermals);
        return (
          <p
            key={name}
            className={`text-xs ${tone === "bad" ? "text-danger" : tone === "warn" ? "text-warning" : "text-muted-foreground"}`}
          >
            {name}: {temp} °C.
            {tone === "bad"
              ? " Check cooling before heavy work."
              : tone === "warn"
                ? " Watch cooling before long jobs."
                : ""}
          </p>
        );
      })}
      {warnings.length > 0 && (
        <details>
          <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">
            Host warnings
          </summary>
          <div className="space-y-2">
            {warnings.map((warning, index) => (
              <p key={index} className="flex gap-2 text-sm">
                <AlertTriangle className="text-warning mt-0.5 size-4 shrink-0" />
                <span>{warning.reason}</span>
              </p>
            ))}
          </div>
        </details>
      )}
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
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" asChild>
              <a href="/status/raw">Technical details</a>
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={refreshAll}
              disabled={statusIsFetching || registryIsFetching}
            >
              <RefreshCcw className="size-4" /> Refresh
            </Button>
          </div>
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
          <div className="space-y-6">
            {Array.from(
              new Set(
                visibleEngines.map((engine) =>
                  engineCapability(engine.service),
                ),
              ),
            ).map((group) => (
              <div key={group}>
                <h3 className="text-muted-foreground mb-2 text-sm font-medium">
                  {group}
                </h3>
                <table className="w-full table-fixed text-sm">
                  <caption className="sr-only">
                    {group} engine status. Select an engine for details.
                  </caption>
                  <thead>
                    <tr className="text-muted-foreground border-b text-left text-xs">
                      <th className="w-3/5 py-2 font-medium sm:w-1/3">
                        Engine
                      </th>
                      <th className="py-2 font-medium sm:w-1/5">State</th>
                      <th className="hidden py-2 font-medium sm:table-cell">
                        Serves
                      </th>
                      <th className="hidden py-2 font-medium md:table-cell">
                        Last error
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleEngines
                      .filter(
                        (engine) => engineCapability(engine.service) === group,
                      )
                      .map((engine) => (
                        <EngineRow
                          key={engine.service.id}
                          engine={engine}
                          selected={engine.service.id === selectedServiceId}
                        />
                      ))}
                  </tbody>
                </table>
              </div>
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
                            : "Configured"
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
                <Link href="/providers">Review connections</Link>
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
