"use client";

import { Suspense, useMemo, useRef, useState } from "react";
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
import { CompactTouchTargets } from "@/components/ui/compact-touch-targets";
import { EngineChecks } from "./engine-checks";
import {
  groupEngines,
  humanCapabilities,
  humanCapability,
} from "./engine-groups";
import { normalizeModelConfigForPatch } from "./detected-models";
import { AddServiceDialog } from "@/components/admin/add-service-dialog";
import { ErrorState } from "@/components/product/error-state";
import { useViewer } from "@/components/shell/session";
import {
  Button,
  ConfirmDialog,
  DialogForm,
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

type Tone = "ok" | "warn" | "bad" | "neutral";
type Backend = NonNullable<StatusPayload["backends"]>[number];

function engineName(service: Service): string {
  return service.name;
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
    return { label: "", tone: "neutral" };
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
        label: "Unavailable",
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
      return { label: "", tone: "neutral" };
    }
  }
  const status = String(
    backend?.live_status ?? backend?.status ?? "",
  ).toLowerCase();
  if (status === "up") return { label: "Ready", tone: "ok" };
  if (status === "degraded") return { label: "Unavailable", tone: "warn" };
  if (status === "down") return { label: "Unavailable", tone: "bad" };
  return { label: "", tone: "neutral" };
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

function advancedJson(service: Service): string {
  return JSON.stringify(
    {
      ...service.config,
      role: service.role,
      priority: service.priority,
      capabilities: service.capabilities,
    },
    null,
    2,
  );
}

function EditEngineDialog({
  service,
  onClose,
  onSaved,
  returnFocus,
  displayName,
  onToggle,
  toggling,
}: {
  service: Service;
  onClose: () => void;
  onSaved: (registry: ServiceRegistry, options?: { close?: boolean }) => void;
  returnFocus: HTMLElement | null;
  displayName: string;
  onToggle: (enabled: boolean) => void;
  toggling: boolean;
}) {
  const [name, setName] = useState(service.name);
  const [baseUrl, setBaseUrl] = useState(composeBaseUrl(service.config));
  const [note, setNote] = useState(String(service.config.note ?? ""));
  const [advanced, setAdvanced] = useState(advancedJson(service));
  const [savedAdvanced, setSavedAdvanced] = useState(advancedJson(service));
  const connectionChanged =
    baseUrl !== composeBaseUrl(service.config) || advanced !== savedAdvanced;
  const [error, setError] = useState<string | null>(null);
  const formRef = useRef<HTMLDivElement>(null);
  const edit = useMutation({
    mutationFn: () => {
      const body =
        service.editable === false
          ? managedUpdateBody(service, name, note)
          : editableUpdateBody(service, name, baseUrl, note, advanced);
      return api.services.edit(service.id, body);
    },
    onSuccess: (registry) => onSaved(registry, { close: true }),
    onError: (err) => setError(errorMessage(err)),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    edit.mutate();
  }

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        className="w-full overflow-y-auto sm:max-w-2xl"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus?.focus();
        }}
      >
        <SheetHeader className="pr-14">
          <SheetTitle>Edit engine</SheetTitle>
          <SheetDescription>{displayName}</SheetDescription>
        </SheetHeader>
        <div ref={formRef} className="space-y-6 px-4 pb-4">
          <label
            htmlFor={`engine-enabled-${service.id}`}
            className="flex min-h-11 items-center gap-3 text-sm"
          >
            <Switch
              id={`engine-enabled-${service.id}`}
              checked={service.enabled}
              aria-label={`Toggle ${displayName}`}
              disabled={toggling}
              onCheckedChange={onToggle}
            />
            Enabled
          </label>
          {connectionChanged && (
            <p role="status" className="text-vf-small text-vf-muted">
              Save configuration changes before testing or detecting models.
            </p>
          )}
          <fieldset disabled={connectionChanged} className="min-w-0">
            <EngineChecks
              service={{ ...service, name: displayName }}
              endpoint={serviceEndpoint(service)}
              onSaved={(registry) => {
                const next = registry.services[service.id];
                if (next) {
                  const latest = advancedJson(next);
                  setAdvanced(latest);
                  setSavedAdvanced(latest);
                }
                onSaved(registry, { close: false });
              }}
              onEdit={() =>
                formRef.current
                  ?.querySelector<HTMLInputElement>("#engine-endpoint")
                  ?.focus()
              }
            />
          </fieldset>
          <DialogForm onSubmit={submit} className="space-y-4">
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
                  id="engine-endpoint"
                  aria-label="Endpoint"
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
              <summary className="flex min-h-11 cursor-pointer items-center text-sm font-medium">
                Connection and models
              </summary>
              <dl className="grid gap-3 py-2">
                <div>
                  <dt className="text-vf-micro text-vf-subtle">
                    Models served
                  </dt>
                  <dd className="text-vf-small">{modelNames(service)}</dd>
                </div>
                <div>
                  <dt className="text-vf-micro text-vf-subtle">Capabilities</dt>
                  <dd className="text-vf-small">
                    {service.capabilities.map(humanCapability).join(", ")}
                  </dd>
                </div>
              </dl>
            </details>
            <details>
              <summary className="flex min-h-11 cursor-pointer items-center text-sm font-medium">
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
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" loading={edit.isPending}>
                Save engine
              </Button>
            </div>
          </DialogForm>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function EnginesContents() {
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const selectedServiceId = searchParams.get("service");
  const [showNotInUse, setShowNotInUse] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Service | null>(null);
  const editInvoker = useRef<HTMLElement | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Service | null>(null);

  function openEditor(service: Service) {
    const row = document.querySelector<HTMLElement>(
      `[data-engine-id="${CSS.escape(service.id)}"]`,
    );
    const active = document.activeElement;
    editInvoker.current =
      active instanceof HTMLElement && row?.contains(active)
        ? active
        : (row?.querySelector<HTMLButtonElement>(
            "button[aria-haspopup='menu']",
          ) ?? null);
    setEditTarget(service);
  }

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
  const services = servicesFromRegistry(registryData);
  const selectedService = selectedServiceId
    ? services.find((service) => service.id === selectedServiceId)
    : undefined;
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

  const infrastructure = services.filter(
    (service) =>
      (service.editable === false || service.source === "provisioned") &&
      humanCapabilities(service.capabilities).length === 0,
  );
  const infrastructureIds = new Set(
    infrastructure.map((service) => service.id),
  );
  const { groups, hiddenCount } = groupEngines(
    services.filter((service) => !infrastructureIds.has(service.id)),
    healthByService,
    showNotInUse,
    selectedServiceId,
  );
  const visibleCount = groups.reduce(
    (count, group) => count + group.entries.length,
    0,
  );

  function displayName(service: Service) {
    return service.display_name || healthByService.get(service.id)?.engine_label || service.name;
  }

  function updateRegistry(registry: ServiceRegistry) {
    queryClient.setQueryData(queryKeys.serviceRegistry, registry);
    setEditTarget((current) =>
      current ? (registry.services[current.id] ?? null) : null,
    );
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

  function renderEngine(service: Service) {
    const backend = backends.get(service.id);
    const health = healthByService.get(service.id);
    const serviceState = state(service, backend, health, observationAvailable);
    const serves = (
      health?.capability_labels ?? humanCapabilities(service.capabilities)
    ).join(", ");
    const title = displayName(service);
    return (
      <article
        key={service.id}
        data-engine-id={service.id}
        className={`rounded-vf-lg border bg-[var(--vf-surface)] px-3 py-2 ${
          service.id === selectedServiceId
            ? "border-[var(--vf-accent)] ring-2 ring-[var(--vf-accent)]/20"
            : "border-[var(--vf-border)]"
        }`}
        aria-current={service.id === selectedServiceId ? "true" : undefined}
      >
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
          <button
            type="button"
            className="min-h-11 min-w-0 text-left"
            onClick={() => openEditor(service)}
          >
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h3 className="text-vf-body text-vf-text truncate font-semibold">
                {title}
              </h3>
              {serviceState.label && serviceState.label !== "Ready" && (
                <StatusDotLabel
                  tone={serviceState.tone}
                  label={serviceState.label}
                />
              )}
            </div>
            {serves && (
              <p className="text-vf-small text-vf-muted mt-1 truncate">
                {serves}
              </p>
            )}
          </button>
          <OverflowMenu label={`Actions for ${title}`}>
            <OverflowMenuItem onSelect={() => openEditor(service)}>
              Edit engine
            </OverflowMenuItem>
            <OverflowMenuItem onSelect={() => openEditor(service)}>
              Test engine
            </OverflowMenuItem>
            <OverflowMenuItem onSelect={() => openEditor(service)}>
              Detect models
            </OverflowMenuItem>
            {service.editable !== false && (
              <>
                <OverflowMenuSeparator />
                <OverflowMenuItem
                  danger
                  onSelect={() => setRemoveTarget(service)}
                >
                  Remove engine
                </OverflowMenuItem>
              </>
            )}
          </OverflowMenu>
        </div>
      </article>
    );
  }

  if (registryIsPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy="true">
        <SectionActionHeader as="h1" title="Engines" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }

  if (registryIsError) {
    return (
      <div className="flex flex-col gap-6">
        <SectionActionHeader as="h1" title="Engines" />
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
        action={
          <Button variant="primary" onClick={() => setAddOpen(true)}>
            Add engine
          </Button>
        }
      />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <label
          htmlFor="show-not-in-use-engines"
          className="flex min-h-11 items-center gap-3 text-sm"
        >
          <Switch
            id="show-not-in-use-engines"
            checked={showNotInUse}
            onCheckedChange={setShowNotInUse}
            aria-label="Show not in use"
          />
          Show not in use ({hiddenCount})
        </label>
      </div>

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

      <section className="flex flex-col gap-3">
        {selectedServiceId && !selectedService && (
          <IncidentBanner
            tone="warn"
            title="Selected engine not found"
            detail={`No service record has id ${selectedServiceId}.`}
          />
        )}
        {visibleCount === 0 && infrastructure.length === 0 ? (
          <EmptyOperationalState
            title={
              services.length === 0
                ? "No engines configured"
                : "No engines in use"
            }
            detail={
              services.length === 0
                ? "Add an engine from a preset, then test its connection and detect served models."
                : "Turn on Show not in use to review engines that are disabled or not configured."
            }
            action={
              <Button variant="primary" onClick={() => setAddOpen(true)}>
                Add engine
              </Button>
            }
          />
        ) : (
          <div className="grid gap-3">
            {groups.map((group) => (
              <section
                key={group.capability}
                className="flex flex-col gap-3"
                aria-label={`${group.capability} engines`}
              >
                <h2 className="text-vf-title font-semibold">
                  {group.capability}
                </h2>
                {group.entries.map(({ service, alternatives }) => (
                  <div key={service.id} className="min-w-0">
                    {renderEngine(service)}
                    {alternatives.length > 0 && (
                      <details
                        className="mt-2"
                        open={
                          alternatives.some(
                            (engine) => engine.id === selectedServiceId,
                          ) || undefined
                        }
                      >
                        <summary className="flex min-h-11 cursor-pointer items-center text-sm font-medium">
                          Standby and fallback engines ({alternatives.length})
                        </summary>
                        <div className="mt-2 grid gap-3">
                          {alternatives.map(renderEngine)}
                        </div>
                      </details>
                    )}
                  </div>
                ))}
              </section>
            ))}
          </div>
        )}
      </section>

      {infrastructure.length > 0 && (
        <details
          open={infrastructureIds.has(selectedServiceId ?? "") || undefined}
        >
          <summary className="flex min-h-11 cursor-pointer items-center text-sm">
            Show infrastructure
          </summary>
          <div className="grid gap-3 pt-2">
            {infrastructure.map(renderEngine)}
          </div>
        </details>
      )}
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
          returnFocus={editInvoker.current}
          displayName={displayName(editTarget)}
          toggling={toggle.isPending}
          onToggle={(enabled) =>
            toggle.mutate({ service: editTarget, enabled })
          }
          onClose={() => setEditTarget(null)}
          onSaved={(registry, options) => {
            updateRegistry(registry);
            if (options?.close) {
              setEditTarget(null);
              toast.success("Engine saved");
            }
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
          <SectionActionHeader as="h1" title="Engines" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      }
    >
      <CompactTouchTargets>
        <EnginesContents />
      </CompactTouchTargets>
    </Suspense>
  );
}
