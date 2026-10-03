"use client";

import { Suspense, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type CurrentHealthService,
  type Service,
  type ServiceRegistry,
  type StatusPayload,
} from "@/lib/api";
import { AddServiceDialog } from "@/components/admin/add-service-dialog";
import { ErrorState } from "@/components/product/error-state";
import { useViewer } from "@/components/shell/session";
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyOperationalState,
  Field,
  IncidentBanner,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  SectionActionHeader,
  Skeleton,
  StatusDotLabel,
  toast,
} from "@/components/ui";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

type Tone = "ok" | "warn" | "bad" | "neutral";
type Backend = NonNullable<StatusPayload["backends"]>[number];

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  completions: "Text",
  image: "Image",
  images: "Image",
  video: "Video",
  speech: "Speech",
  tts: "Speech",
  transcribe: "Transcribe",
  transcription: "Transcribe",
  stt: "Transcribe",
  embeddings: "Embeddings",
  embed: "Embeddings",
  rerank: "Rerank",
  decide: "Decide",
  decisions: "Decide",
};

function humanCapability(capability: string): string {
  return (
    CAPABILITY_LABELS[capability.toLowerCase()] ??
    capability
      .replace(/[._-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
}

function engineName(service: Service): string {
  const labels = Array.from(new Set(service.capabilities.map(humanCapability)));
  const primary = labels.length > 0 ? labels.join(" & ") : "Engine";
  const technical =
    typeof service.config.protocol === "string" && service.config.protocol
      ? service.config.protocol
      : service.kind;
  return `${primary} (${technical})`;
}

function composeBaseUrl(config: Service["config"]): string {
  if (typeof config.base_url === "string" && config.base_url) {
    return config.base_url;
  }
  if (typeof config.host === "string" && config.host) {
    const port =
      typeof config.port === "number" || typeof config.port === "string"
        ? `:${config.port}`
        : "";
    const path = typeof config.base_path === "string" ? config.base_path : "";
    return `http://${config.host}${port}${path}`;
  }
  return "";
}

function serviceEndpoint(service: Service): string {
  return composeBaseUrl(service.config) || "Not configured";
}

function modelNames(service: Service): string {
  const models = service.config.models;
  if (Array.isArray(models)) {
    if (models.length === 0) return "No served models configured";
    const names = models
      .map((model) => {
        if (typeof model === "object" && model) {
          const record = model as unknown as Record<string, unknown>;
          return String(
            record.model_group ?? record.id ?? record.upstream_model ?? "",
          );
        }
        return String(model ?? "");
      })
      .filter(Boolean);
    return names.length ? names.join(", ") : "No served models configured";
  }
  if (models && typeof models === "object") {
    const names = Object.keys(models);
    return names.length ? names.join(", ") : "No served models configured";
  }
  return "No served models configured";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeModelConfigForPatch(value: unknown): unknown {
  if (Array.isArray(value)) return value;
  if (!isRecord(value)) return value;
  return Object.entries(value).map(([modelName, modelConfig]) => {
    if (isRecord(modelConfig)) {
      const id = modelConfig.id || modelName;
      const normalized: Record<string, unknown> = {
        ...modelConfig,
        id,
        model_group: modelConfig.model_group || id,
        upstream_model:
          modelConfig.upstream_model ||
          modelConfig.deployment ||
          modelConfig.model ||
          id,
      };
      delete normalized.public;
      delete normalized.upstream;
      return normalized;
    }
    return {
      id: modelName,
      model_group: modelName,
      upstream_model: String(modelConfig || modelName),
    };
  });
}

function endpointFields(baseUrl: string): Record<string, unknown> {
  const text = baseUrl.trim();
  if (!text) throw new Error("Endpoint is required.");
  const parsed = new URL(text.includes("://") ? text : `http://${text}`);
  if (parsed.protocol !== "http:") {
    throw new Error(
      "Endpoint must use http://. HTTPS endpoints are not supported by this registry editor yet.",
    );
  }
  const defaultPort = 80;
  const path =
    parsed.pathname && parsed.pathname !== "/"
      ? parsed.pathname.replace(/\/$/, "")
      : "/";
  return {
    host: parsed.hostname,
    port: parsed.port ? Number(parsed.port) : defaultPort,
    base_path: path,
  };
}

function tone(value: unknown): Tone {
  return value === "ok" || value === "warn" || value === "bad"
    ? value
    : "neutral";
}

function observedTone(
  value: unknown,
  fallback: Exclude<Tone, "neutral">,
): Tone {
  const next = tone(value);
  return next === "neutral" ? fallback : next;
}

function state(
  service: Service,
  backend: Backend | undefined,
  health: CurrentHealthService | undefined,
  observationAvailable: boolean,
): { label: string; tone: Tone } {
  if (!service.enabled) return { label: "Disabled", tone: "neutral" };
  if (!observationAvailable) {
    return { label: "Not measured yet", tone: "neutral" };
  }
  if (health) {
    if (health.observation === "up") {
      const healthTone = observedTone(health.tone, "ok");
      return healthTone === "bad"
        ? { label: "Unavailable", tone: "bad" }
        : healthTone === "warn"
          ? { label: "Ready with warning", tone: "warn" }
          : { label: "Ready", tone: healthTone };
    }
    if (health.observation === "degraded") {
      return {
        label: "Unavailable: probe reports degraded",
        tone: observedTone(health.tone, "warn"),
      };
    }
    if (health.observation === "down") {
      return { label: "Unavailable", tone: observedTone(health.tone, "bad") };
    }
    if (health.observation === "standby_stopped") {
      return {
        label: "Idle",
        tone: "neutral",
      };
    }
    if (health.observation === "idle") {
      return { label: "Ready to start", tone: "neutral" };
    }
    if (health.observation === "busy") {
      return { label: "Busy", tone: observedTone(health.tone, "warn") };
    }
    if (health.observation === "starting") {
      return { label: "Starting", tone: "neutral" };
    }
    if (health.observation === "unavailable") {
      return { label: "Not measured yet", tone: "neutral" };
    }
  }
  const status = String(
    backend?.live_status ?? backend?.status ?? "",
  ).toLowerCase();
  if (status === "up") return { label: "Ready", tone: "ok" };
  if (status === "degraded")
    return { label: "Unavailable: probe reports degraded", tone: "warn" };
  if (status === "down") return { label: "Unavailable", tone: "bad" };
  return { label: "Not measured yet", tone: "neutral" };
}

function servicesFromRegistry(
  registry: ServiceRegistry | undefined,
): Service[] {
  return Object.values(registry?.services ?? {}).sort((a, b) =>
    engineName(a).localeCompare(engineName(b)),
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong. Please try again.";
}

function editableUpdateBody(
  service: Service,
  name: string,
  baseUrl: string,
  note: string,
  advanced: string,
): Record<string, unknown> {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(advanced);
  } catch {
    throw new Error("Advanced fields must be valid JSON.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Advanced fields must be a JSON object.");
  }
  if ("models" in parsed) {
    parsed.models = normalizeModelConfigForPatch(parsed.models);
  }
  let endpoint: Record<string, unknown>;
  try {
    endpoint = endpointFields(baseUrl);
  } catch (err) {
    if (err instanceof Error) throw err;
    throw new Error("Endpoint must be a valid URL.");
  }
  const nextConfig: Record<string, unknown> = {
    ...parsed,
    ...endpoint,
    name: name.trim() || service.name,
    enabled: service.enabled,
    note: note.trim(),
  };
  delete nextConfig.base_url;
  return nextConfig;
}

function managedUpdateBody(
  service: Service,
  name: string,
  note: string,
): Record<string, unknown> {
  return {
    name: name.trim() || service.name,
    note: note.trim(),
  };
}

function EditEngineDialog({
  service,
  onClose,
  onSaved,
}: {
  service: Service;
  onClose: () => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const [name, setName] = useState(service.name);
  const [baseUrl, setBaseUrl] = useState(composeBaseUrl(service.config));
  const [note, setNote] = useState(String(service.config.note ?? ""));
  const [advanced, setAdvanced] = useState(
    JSON.stringify(
      {
        ...service.config,
        role: service.role,
        priority: service.priority,
        capabilities: service.capabilities,
      },
      null,
      2,
    ),
  );
  const [error, setError] = useState<string | null>(null);
  const edit = useMutation({
    mutationFn: () => {
      const body =
        service.editable === false
          ? managedUpdateBody(service, name, note)
          : editableUpdateBody(service, name, baseUrl, note, advanced);
      return api.services.edit(service.id, body);
    },
    onSuccess: onSaved,
    onError: (err) => setError(errorMessage(err)),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    edit.mutate();
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogForm onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>Edit engine</DialogTitle>
            <DialogDescription>
              Save changes to this engine’s configuration.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-4">
              {error && (
                <IncidentBanner
                  tone="bad"
                  title="Engine could not be saved"
                  detail={error}
                />
              )}
              <Field label="Name" required>
                <Input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                />
              </Field>
              {service.editable !== false && (
                <Field label="Endpoint">
                  <Input
                    value={baseUrl}
                    onChange={(event) => setBaseUrl(event.target.value)}
                  />
                </Field>
              )}
              <Field label="Note">
                <Textarea
                  aria-label="Note"
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </Field>
              <details>
                <summary className="cursor-pointer text-sm font-medium">
                  Advanced fields
                </summary>
                {service.editable === false ? (
                  <pre className="text-vf-micro rounded-vf-md mt-3 max-h-72 overflow-auto bg-[var(--vf-surface-2)] p-3 font-mono">
                    {JSON.stringify(service.config, null, 2)}
                  </pre>
                ) : (
                  <Field
                    label="Advanced JSON"
                    help="Edit service config, role, priority, and capabilities here. Name, endpoint, enabled state, and note use their dedicated controls."
                  >
                    <Textarea
                      aria-label="Advanced JSON"
                      className="mt-2 min-h-36 font-mono"
                      value={advanced}
                      onChange={(event) => setAdvanced(event.target.value)}
                    />
                  </Field>
                )}
              </details>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={edit.isPending}>
              Save engine
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    </Dialog>
  );
}

function EnginesContents() {
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const [showDisabled, setShowDisabled] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Service | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Service | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);

  const {
    data: registryData,
    error: registryError,
    isError: registryIsError,
    isPending: registryIsPending,
    refetch: refetchRegistry,
  } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });
  const {
    data: rawStatusData,
    error: statusError,
    isError: statusIsError,
    refetch: refetchStatus,
  } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
  const { data: presetsData } = useQuery({
    queryKey: queryKeys.servicePresets,
    queryFn: api.services.presets,
  });

  const services = servicesFromRegistry(registryData);
  const selectedService = selectedServiceId
    ? services.find((service) => service.id === selectedServiceId)
    : undefined;
  const visibleServices = showDisabled
    ? services
    : services.filter(
        (service) => service.enabled || service.id === selectedServiceId,
      );
  const statusData = statusIsError ? undefined : rawStatusData;
  const backends = useMemo(
    () =>
      new Map(
        (statusData?.backends ?? []).flatMap((backend) =>
          backend.service_id ? [[backend.service_id, backend] as const] : [],
        ),
      ),
    [statusData],
  );
  const healthByService = useMemo(
    () =>
      new Map(
        (statusData?.current_health?.services ?? []).map((service) => [
          service.service_id,
          service,
        ]),
      ),
    [statusData],
  );
  const observationAvailable =
    statusData?.current_health?.observation_available === true;

  function updateRegistry(registry: ServiceRegistry) {
    queryClient.setQueryData(queryKeys.serviceRegistry, registry);
    void queryClient.invalidateQueries({ queryKey: queryKeys.status });
  }

  const toggle = useMutation({
    mutationFn: ({
      service,
      enabled,
    }: {
      service: Service;
      enabled: boolean;
    }) => api.services.toggleEnabled(service.id, enabled),
    onSuccess: updateRegistry,
    onError: (err) =>
      toast.error("Engine could not be updated", {
        description: errorMessage(err),
      }),
  });

  const remove = useMutation({
    mutationFn: (service: Service) => api.services.remove(service.id),
    onSuccess: (registry) => {
      updateRegistry(registry);
      setRemoveTarget(null);
      toast.success("Engine removed");
    },
    onError: (err) =>
      toast.error("Engine could not be removed", {
        description: errorMessage(err),
      }),
  });

  const test = useMutation({
    mutationFn: (service: Service) =>
      api.services.testConnection({
        base_url: service.config.base_url,
        host: service.config.host,
        port: service.config.port,
        base_path: service.config.base_path,
        health_endpoint: service.config.health_endpoint,
      }),
    onSuccess: (probe) =>
      setTestResult(
        probe.ok
          ? `Connected to ${probe.url} (${probe.status ?? "ok"}).`
          : probe.error || "Connection failed.",
      ),
    onError: (err) => setTestResult(errorMessage(err)),
  });

  const detect = useMutation({
    mutationFn: (service: Service) =>
      api.services.detectModels({
        base_url: service.config.base_url,
        host: service.config.host,
        port: service.config.port,
        base_path: service.config.base_path,
      }),
    onSuccess: (detected) =>
      setTestResult(
        detected.ok
          ? `Detected models: ${
              detected.models.length ? detected.models.join(", ") : "none"
            }`
          : detected.error || "No models detected.",
      ),
    onError: (err) => setTestResult(errorMessage(err)),
  });

  if (registryIsPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy="true">
        <SectionActionHeader
          as="h1"
          title="Engines"
          description="Loading local engines."
        />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  if (registryIsError) {
    return (
      <div className="flex flex-col gap-6">
        <SectionActionHeader
          as="h1"
          title="Engines"
          description="The local engines that power your AI."
        />
        <IncidentBanner
          tone="bad"
          title="Engines unavailable"
          detail={errorMessage(registryError)}
          actions={
            <Button size="sm" onClick={() => void refetchRegistry()}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex max-w-[1120px] flex-col gap-8">
      <SectionActionHeader
        as="h1"
        title="Engines"
        description="These local engines are available to the gateway."
        action={
          <Button variant="primary" onClick={() => setAddOpen(true)}>
            Add engine
          </Button>
        }
      />

      <div className="rounded-vf-lg flex flex-wrap items-center justify-between gap-3 border border-[var(--vf-border)] bg-[var(--vf-surface)] p-4">
        <div>
          <p className="text-vf-body text-vf-text font-medium">
            {visibleServices.length} shown · {services.length} total
          </p>
          <p className="text-vf-small text-vf-muted">
            Disabled engines are hidden by default.
          </p>
        </div>
        <label
          htmlFor="show-disabled-engines"
          className="flex min-h-11 items-center gap-3 text-sm"
        >
          <Switch
            id="show-disabled-engines"
            checked={showDisabled}
            onCheckedChange={setShowDisabled}
            aria-label="Show disabled"
          />
          Show disabled
        </label>
      </div>

      {testResult && (
        <IncidentBanner
          tone="info"
          title="Engine check complete"
          detail={testResult}
          onDismiss={() => setTestResult(null)}
        />
      )}

      {statusIsError && (
        <IncidentBanner
          tone="warn"
          title="Live status unavailable"
          detail={errorMessage(statusError)}
          actions={
            <Button size="sm" onClick={() => void refetchStatus()}>
              Retry
            </Button>
          }
        />
      )}

      {presetsData?.presets?.length ? (
        <section className="flex flex-col gap-3">
          <SectionActionHeader
            title="Available presets"
            description="Choose a preset when adding an engine."
          />
          <div className="flex flex-wrap gap-2">
            {presetsData.presets.map((preset) => (
              <Badge key={preset.id} tone="neutral">
                {preset.label}
              </Badge>
            ))}
          </div>
        </section>
      ) : null}

      <section className="flex flex-col gap-3">
        <SectionActionHeader title="Local engines" />
        {selectedServiceId && !selectedService && (
          <IncidentBanner
            tone="warn"
            title="Selected engine not found"
            detail={`No service record has id ${selectedServiceId}.`}
          />
        )}
        {visibleServices.length === 0 ? (
          <EmptyOperationalState
            title={
              services.length === 0
                ? "No engines configured"
                : "No enabled engines shown"
            }
            detail={
              services.length === 0
                ? "Add an engine from a preset, then test its connection and detect served models."
                : "Turn on Show disabled to review disabled entries."
            }
            action={
              <Button variant="primary" onClick={() => setAddOpen(true)}>
                Add engine
              </Button>
            }
          />
        ) : (
          <div className="grid gap-3">
            {visibleServices.map((service) => {
              const backend = backends.get(service.id);
              const health = healthByService.get(service.id);
              const serviceState = state(
                service,
                backend,
                health,
                observationAvailable,
              );
              return (
                <article
                  key={service.id}
                  className={`rounded-vf-xl border bg-[var(--vf-surface)] p-5 ${
                    service.id === selectedServiceId
                      ? "border-[var(--vf-accent)] ring-2 ring-[var(--vf-accent)]/20"
                      : "border-[var(--vf-border)]"
                  }`}
                  aria-current={
                    service.id === selectedServiceId ? "true" : undefined
                  }
                >
                  <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                    <div className="min-w-0">
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <h2 className="text-vf-title text-vf-text font-semibold">
                          {engineName(service)}
                        </h2>
                        <StatusDotLabel
                          tone={serviceState.tone}
                          label={serviceState.label}
                        />
                      </div>
                      <p className="text-vf-small text-vf-muted">
                        {service.name}
                      </p>
                      <p className="text-vf-small text-vf-subtle mt-1 font-mono break-all">
                        {serviceEndpoint(service)}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Switch
                        id={`engine-enabled-${service.id}`}
                        checked={service.enabled}
                        aria-label={`Toggle ${service.name}`}
                        disabled={toggle.isPending}
                        onCheckedChange={(enabled) =>
                          toggle.mutate({ service, enabled })
                        }
                      />
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={test.isPending}
                        onClick={() => test.mutate(service)}
                      >
                        Test
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={detect.isPending}
                        onClick={() => detect.mutate(service)}
                      >
                        Detect
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setEditTarget(service)}
                      >
                        Edit
                      </Button>
                      {service.editable !== false && (
                        <OverflowMenu label={`Actions for ${service.name}`}>
                          <OverflowMenuSeparator />
                          <OverflowMenuItem
                            danger
                            onSelect={() => setRemoveTarget(service)}
                          >
                            Remove engine
                          </OverflowMenuItem>
                        </OverflowMenu>
                      )}
                    </div>
                  </div>
                  <dl className="mt-5 grid gap-4 md:grid-cols-3">
                    <div>
                      <dt className="text-vf-micro text-vf-subtle">Role</dt>
                      <dd className="text-vf-small text-vf-text">
                        {service.role ?? "optional"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-vf-micro text-vf-subtle">
                        Capabilities
                      </dt>
                      <dd className="text-vf-small text-vf-text">
                        {service.capabilities.map(humanCapability).join(", ") ||
                          "No capabilities configured"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-vf-micro text-vf-subtle">
                        Served models
                      </dt>
                      <dd className="text-vf-small text-vf-text">
                        {modelNames(service)}
                      </dd>
                    </div>
                  </dl>
                  <details className="mt-4">
                    <summary className="cursor-pointer text-sm font-medium">
                      Advanced
                    </summary>
                    <pre className="text-vf-micro rounded-vf-md mt-3 max-h-72 overflow-auto bg-[var(--vf-surface-2)] p-3 font-mono">
                      {JSON.stringify(
                        {
                          id: service.id,
                          kind: service.kind,
                          source: service.source,
                          config: service.config,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <AddServiceDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onSaved={(registry) => {
          updateRegistry(registry);
          setAddOpen(false);
          toast.success("Engine added");
        }}
      />
      {editTarget && (
        <EditEngineDialog
          service={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={(registry) => {
            updateRegistry(registry);
            setEditTarget(null);
            toast.success("Engine saved");
          }}
        />
      )}
      <ConfirmDialog
        open={Boolean(removeTarget)}
        title="Remove engine?"
        description={
          removeTarget
            ? `${removeTarget.name} will be removed from the service registry.`
            : ""
        }
        confirmLabel="Remove engine"
        danger
        typedName={removeTarget?.name}
        onOpenChange={(open) => !open && setRemoveTarget(null)}
        onConfirm={() => {
          if (!removeTarget) return;
          return remove.mutateAsync(removeTarget).then(() => undefined);
        }}
      />
    </div>
  );
}

export default function Page() {
  const viewer = useViewer();
  if (!viewer.admin) {
    return (
      <ErrorState message="This page is available to gateway owners and admins. Sign in with an owner account to continue." />
    );
  }
  return (
    <Suspense
      fallback={
        <div className="flex flex-col gap-6" aria-busy="true">
          <SectionActionHeader
            as="h1"
            title="Engines"
            description="Loading local engines."
          />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      }
    >
      <EnginesContents />
    </Suspense>
  );
}
