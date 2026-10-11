"use client";

import { Fragment, Suspense, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";

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
import { restoreFocus } from "@/lib/restore-focus";
import { AddServiceDialog } from "@/components/admin/add-service-dialog";
import { ErrorState } from "@/components/product/error-state";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { EmptyState } from "@/components/product/empty-state";
import { Callout } from "@/components/product/status-ui";
import { Input } from "@/components/ui/input";
import {
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
} from "@/components/ui/overflow-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { PageHeader } from "@/components/product/page-header";
import { StateBadge, StateWord } from "@/components/product/status-ui";
import { RelativeTime } from "@/components/ui/relative-time";
import { Table, TableBody, TableRow, TableCell } from "@/components/ui/table";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

import {
  engineDisplayName,
  engineHealthPresentation,
} from "@/lib/health-presentation";

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
  // A new or changed endpoint must be http: health polling builds
  // http://host:port, so an https engine would route but never look healthy.
  if (parsed.protocol !== "http:") {
    throw new Error(
      "Endpoint must use http://. HTTPS endpoints are not supported by this editor yet.",
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

function state(
  service: Service,
  backend: Backend | undefined,
  health: CurrentHealthService | undefined,
  observationAvailable: boolean,
) {
  return engineHealthPresentation(
    service.enabled,
    observationAvailable,
    health,
    backend?.live_status ?? backend?.status,
  );
}

function servicesFromRegistry(
  registry: ServiceRegistry | undefined,
): Service[] {
  return Object.values(registry?.services ?? {}).sort((a, b) =>
    engineName(a).localeCompare(engineName(b)),
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412)
      return "Engine settings changed elsewhere. Refresh and try again.";
    // The exact reason stays under Technical details.
    if (error.status === 400 || error.status === 409)
      return "The gateway did not accept these engine settings. Check the fields and save again.";
  } else if (error instanceof Error && !(error instanceof TypeError)) {
    // This editor's own check, such as an https:// address: say it as is.
    return error.message;
  }
  return "Engine settings could not be saved. We could not identify the cause. Try saving again.";
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
  let endpoint: Record<string, unknown> = {};
  try {
    if (baseUrl !== composeBaseUrl(service.config)) {
      endpoint = endpointFields(baseUrl);
      delete parsed.base_url;
    }
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
  fallbackFocus,
  displayName,
}: {
  service: Service;
  onClose: () => void;
  onSaved: (registry: ServiceRegistry, options?: { close?: boolean }) => void;
  returnFocus: HTMLElement | null;
  fallbackFocus: () => readonly (HTMLElement | null)[];
  displayName: string;
}) {
  const [enabled, setEnabled] = useState(service.enabled);
  const [name, setName] = useState(service.name);
  const [baseUrl, setBaseUrl] = useState(composeBaseUrl(service.config));
  const [note, setNote] = useState(String(service.config.note ?? ""));
  const [advanced, setAdvanced] = useState(advancedJson(service));
  const [savedAdvanced, setSavedAdvanced] = useState(advancedJson(service));
  const connectionChanged =
    baseUrl !== composeBaseUrl(service.config) || advanced !== savedAdvanced;
  const [error, setError] = useState<string | null>(null);
  const [technicalError, setTechnicalError] = useState<string | null>(null);
  const formRef = useRef<HTMLDivElement>(null);
  const edit = useMutation({
    mutationFn: () => {
      const body =
        service.editable === false
          ? managedUpdateBody(service, name, note)
          : editableUpdateBody(service, name, baseUrl, note, advanced);
      return api.services.edit(service.id, { ...body, enabled });
    },
    onSuccess: (registry) => onSaved(registry, { close: true }),
    onError: (err) => {
      setError(errorMessage(err));
      setTechnicalError(
        err instanceof ApiError || err instanceof TypeError ? err.message : null,
      );
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setTechnicalError(null);
    edit.mutate();
  }

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        className="w-full overflow-y-auto sm:max-w-2xl"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          restoreFocus(returnFocus, fallbackFocus);
        }}
      >
        <SheetHeader className="pr-14">
          <SheetTitle>Edit engine</SheetTitle>
          <SheetDescription>{displayName}</SheetDescription>
        </SheetHeader>
        <div ref={formRef} className="space-y-6 px-4 pb-4">
          <label
            htmlFor={`engine-enabled-${service.id}`}
            className="text-meta flex min-h-8 items-center gap-3 max-sm:min-h-11"
          >
            <Switch
              id={`engine-enabled-${service.id}`}
              checked={enabled}
              aria-label={`Toggle ${displayName}`}
              disabled={edit.isPending}
              onCheckedChange={setEnabled}
            />
            Enabled
          </label>
          {connectionChanged && (
            <p role="status" className="text-meta text-muted-foreground">
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
          <form
            onSubmit={submit}
            className="flex min-h-0 flex-1 flex-col space-y-4"
          >
            {error && (
              <Callout
                as="div"
                severity="critical"
                title="Engine could not be saved"
                meta={error}
              />
            )}
            {technicalError && (
              <details>
                <summary>Technical details</summary>
                <p className="break-words">{technicalError}</p>
              </details>
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
              <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
                Connection and models
              </summary>
              <dl className="grid gap-3 py-2">
                <div>
                  <dt className="text-meta text-muted-foreground">
                    Models served
                  </dt>
                  <dd className="text-meta">{modelNames(service)}</dd>
                </div>
                <div>
                  <dt className="text-meta text-muted-foreground">
                    Capabilities
                  </dt>
                  <dd className="text-meta">
                    {service.capabilities.map(humanCapability).join(", ")}
                  </dd>
                </div>
              </dl>
            </details>
            <details>
              <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
                Advanced fields
              </summary>
              {service.editable === false ? (
                <pre className="text-meta rounded-control bg-muted mt-3 max-h-72 overflow-auto p-3 font-mono">
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
              <Button size="md" type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button
                disabled={edit.isPending}
                size="md"
                type="submit"
                variant="default"
                aria-busy={edit.isPending || undefined}
              >
                {edit.isPending && <Spinner className="size-4" />}
                Save engine
              </Button>
            </div>
          </form>
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
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showInfrastructure, setShowInfrastructure] = useState<boolean>();
  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Service | null>(null);
  const editInvoker = useRef<HTMLElement | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Service | null>(null);
  const removeInvoker = useRef<HTMLElement | null>(null);
  const addEngineButton = useRef<HTMLButtonElement | null>(null);
  const focusNeighbours = useRef<HTMLElement[]>([]);

  function captureFocusNeighbours(service: Service) {
    const actions = Array.from(
      document.querySelectorAll<HTMLElement>(
        "[data-engine-id] button[aria-haspopup='menu']",
      ),
    );
    const index = actions.findIndex(
      (button) =>
        button.closest("[data-engine-id]")?.getAttribute("data-engine-id") ===
        service.id,
    );
    focusNeighbours.current =
      index < 0
        ? actions
        : [...actions.slice(index + 1), ...actions.slice(0, index).reverse()];
  }

  function fallbackFocus() {
    return [...focusNeighbours.current, addEngineButton.current];
  }

  function openEditor(service: Service) {
    captureFocusNeighbours(service);
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
    return engineDisplayName(service, healthByService.get(service.id));
  }

  function updateRegistry(registry: ServiceRegistry) {
    queryClient.setQueryData(queryKeys.serviceRegistry, registry);
    setEditTarget((current) =>
      current ? (registry.services[current.id] ?? null) : null,
    );
    void queryClient.invalidateQueries({ queryKey: queryKeys.status });
  }

  const remove = useMutation({
    mutationFn: (service: Service) => api.services.remove(service.id),
    onSuccess: (registry) => {
      updateRegistry(registry);
      setRemoveTarget(null);
      toast.success("Engine removed");
    },
    onError: (err) =>
      toast.error("Engine could not be removed", {
        description: err instanceof ApiError && err.status === 412
          ? "Engine settings changed elsewhere. Refresh and try again."
          : "We could not identify the cause. Try removing the engine again.",
      }),
  });

  function renderEngine(service: Service) {
    const backend = backends.get(service.id);
    const health = healthByService.get(service.id);
    const serviceState = state(service, backend, health, observationAvailable);
    const title = displayName(service);
    // The group heading names the first capability; list only the others.
    const alsoServes = humanCapabilities(service.capabilities).slice(1).join(", ");
    const stateKey =
      !service.enabled || serviceState.label === "Disabled"
        ? "off"
        : ["Ready", "Ready to start", "Standby, stopped"].includes(
              serviceState.label,
            )
          ? "ready"
          : ["Busy", "Starting"].includes(serviceState.label)
            ? "busy"
            : serviceState.tone === "bad"
              ? "down"
              : serviceState.tone === "warn"
                ? "degraded"
                : "unknown";
    // Use the poller's record time, never the payload generation time.
    const observedAt = (
      backend as { observed_at_s?: number | null } | undefined
    )?.observed_at_s;
    const checkedAt =
      typeof observedAt === "number" &&
      Number.isFinite(observedAt) &&
      observedAt > 0
        ? observedAt
        : null;
    return (
      <TableRow
        key={service.id}
        data-engine-id={service.id}
        className="h-(--row-2) max-sm:h-(--row-phone-2)"
        data-state={service.id === selectedServiceId ? "selected" : undefined}
        aria-current={service.id === selectedServiceId ? "true" : undefined}
      >
        <TableCell className="w-full max-w-0 py-1!">
          <button
            type="button"
            className="flex min-h-8 w-full min-w-0 flex-col justify-center text-left max-sm:min-h-11"
            onClick={() => openEditor(service)}
          >
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="text-body text-foreground min-w-0 truncate font-medium">
                {title}
              </h3>
              <span className="flex shrink-0">
                {stateKey === "ready" && serviceState.label === "Ready" ? (
                  <StateWord tone="ok" word="Ready" />
                ) : (
                  <StateBadge
                    state={stateKey}
                    word={
                      stateKey === "off"
                        ? undefined
                        : serviceState.label || undefined
                    }
                  />
                )}
              </span>
            </div>
            {(alsoServes || checkedAt !== null) && (
              <p className="text-meta text-muted-foreground mt-0.5 truncate">
                {alsoServes ? `Also ${alsoServes}` : null}
                {alsoServes && checkedAt !== null ? " · " : null}
                {checkedAt !== null ? (
                  <>
                    Last checked <RelativeTime ts={checkedAt} fallback="" />
                  </>
                ) : null}
              </p>
            )}
          </button>
        </TableCell>
        <TableCell className="w-12 text-right">
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
                  onSelect={() => {
                    captureFocusNeighbours(service);
                    removeInvoker.current = document.querySelector<HTMLElement>(
                      `[data-engine-id="${CSS.escape(service.id)}"] button[aria-haspopup='menu']`,
                    );
                    setRemoveTarget(service);
                  }}
                >
                  Remove engine
                </OverflowMenuItem>
              </>
            )}
          </OverflowMenu>
        </TableCell>
      </TableRow>
    );
  }

  if (registryIsPending) {
    return (
      <div className="flex flex-col gap-6" aria-busy="true">
        <PageHeader title="Engines" />
        <Skeleton aria-hidden="true" className="h-20 w-full" />
        <Skeleton aria-hidden="true" className="h-72 w-full" />
      </div>
    );
  }

  if (registryIsError) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title="Engines" />
        <Callout
          as="div"
          severity="critical"
          title="Engine list could not be loaded"
          meta="Each engine's state is unknown. Try loading the engine list again."
          actions={
            <Button
              variant="outline"
              type="button"
              size="sm"
              onClick={() => void refetchRegistry()}
            >
              Retry
            </Button>
          }
        />
        {registryError instanceof Error && (
          <details>
            <summary>Technical details</summary>
            <p className="break-words">{registryError.message}</p>
          </details>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-(--content-max)">
      <PageHeader
        title="Engines"
        actions={
          <Button
            ref={addEngineButton}
            type="button"
            size="sm"
            variant="default"
            onClick={() => setAddOpen(true)}
          >
            Add engine
          </Button>
        }
      />

      <div className="space-y-(--space-section)">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label
            htmlFor="show-not-in-use-engines"
            className="text-meta flex min-h-8 items-center gap-3 max-sm:min-h-11"
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
          <div role="status">
            <Callout
              as="div"
              severity="attention"
              title="Live status unavailable"
              meta="Engine states are unknown. Try loading live status again."
              actions={
                <Button
                  variant="outline"
                  type="button"
                  size="sm"
                  onClick={() => void refetchStatus()}
                >
                  Retry
                </Button>
              }
            />
            {statusError instanceof Error && (
              <details>
                <summary>Technical details</summary>
                <p className="break-words">{statusError.message}</p>
              </details>
            )}
          </div>
        )}

        <section className="flex flex-col gap-3">
          {selectedServiceId && !selectedService && (
            <div role="status">
              <Callout
                as="div"
                severity="attention"
                title="Selected engine not found"
                meta="This engine is no longer available. Choose another engine."
              />
            </div>
          )}
          {visibleCount === 0 && infrastructure.length === 0 ? (
            <EmptyState
              title={
                services.length === 0
                  ? "No engines configured"
                  : "No engines in use"
              }
              description={
                services.length === 0
                  ? "Add an engine from a preset, then test its connection and detect served models."
                  : "Turn on Show not in use to review engines that are disabled or not configured."
              }
              action={
                <Button
                  size="md"
                  type="button"
                  variant="default"
                  onClick={() => setAddOpen(true)}
                >
                  Add engine
                </Button>
              }
            />
          ) : (
            <div className="bg-card rounded-panel overflow-hidden border">
              <Table aria-label="Engines">
                {groups.map((group) => (
                  <TableBody
                    key={group.capability}
                    aria-label={`${group.capability} engines`}
                  >
                    <TableRow className="bg-muted/40 hover:bg-muted/40">
                      <TableCell
                        colSpan={2}
                        className="h-9 max-sm:h-(--row-phone-1)!"
                      >
                        <h2 className="text-label text-subtle-foreground font-medium">
                          {group.capability}
                        </h2>
                      </TableCell>
                    </TableRow>
                    {group.entries.map(({ service, alternatives }) => {
                      const open =
                        expanded[service.id] ??
                        alternatives.some(
                          (engine) => engine.id === selectedServiceId,
                        );
                      return (
                        <Fragment key={service.id}>
                          {renderEngine(service)}
                          {alternatives.length > 0 && (
                            <TableRow>
                              <TableCell
                                colSpan={2}
                                className="h-(--row-1) py-0! max-sm:h-(--row-phone-1)!"
                              >
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  aria-expanded={open}
                                  onClick={() =>
                                    setExpanded((current) => ({
                                      ...current,
                                      [service.id]: !open,
                                    }))
                                  }
                                >
                                  Standby and fallback engines (
                                  {alternatives.length})
                                </Button>
                              </TableCell>
                            </TableRow>
                          )}
                          {open && alternatives.map(renderEngine)}
                        </Fragment>
                      );
                    })}
                  </TableBody>
                ))}
                {infrastructure.length > 0 && (
                  <TableBody aria-label="Infrastructure engines">
                    <TableRow>
                      <TableCell
                        colSpan={2}
                        className="h-(--row-1) py-0! max-sm:h-(--row-phone-1)!"
                      >
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          aria-expanded={
                            showInfrastructure ??
                            infrastructureIds.has(selectedServiceId ?? "")
                          }
                          onClick={() =>
                            setShowInfrastructure(
                              !(
                                showInfrastructure ??
                                infrastructureIds.has(selectedServiceId ?? "")
                              ),
                            )
                          }
                        >
                          Show infrastructure
                        </Button>
                      </TableCell>
                    </TableRow>
                    {(showInfrastructure ??
                      infrastructureIds.has(selectedServiceId ?? "")) &&
                      infrastructure.map(renderEngine)}
                  </TableBody>
                )}
              </Table>
            </div>
          )}
        </section>
      </div>
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
          fallbackFocus={fallbackFocus}
          displayName={displayName(editTarget)}
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
            ? `Remove ${removeTarget.name} from this gateway?`
            : ""
        }
        confirmLabel="Remove engine"
        danger
        typedName={removeTarget?.name}
        returnFocus={removeInvoker.current}
        fallbackFocus={fallbackFocus}
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
      <ErrorState message="Only an owner or admin can manage engines. Ask an owner or admin for help." />
    );
  }
  return (
    <Suspense
      fallback={
        <div className="flex flex-col gap-6" aria-busy="true">
          <PageHeader title="Engines" />
          <Skeleton aria-hidden="true" className="h-20 w-full" />
          <Skeleton aria-hidden="true" className="h-72 w-full" />
        </div>
      }
    >
      <CompactTouchTargets>
        <EnginesContents />
      </CompactTouchTargets>
    </Suspense>
  );
}
