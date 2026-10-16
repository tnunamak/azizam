"use client";

import { Fragment, Suspense, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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
import { groupEngines, humanCapabilities } from "./engine-groups";
import { AddEngineDialog } from "./add-engine/add-engine-dialog";
import { EngineRecordPage } from "./record/engine-record-page";
import { ErrorState } from "@/components/product/error-state";
import { useViewer } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/product/empty-state";
import { Callout } from "@/components/product/status-ui";
import {
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
} from "@/components/ui/overflow-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/product/page-header";
import { StateBadge, StateWord } from "@/components/product/status-ui";
import { RelativeTime } from "@/components/ui/relative-time";
import { Table, TableBody, TableRow, TableCell } from "@/components/ui/table";
import { Switch } from "@/components/ui/switch";

import {
  engineDisplayName,
  engineHealthPresentation,
} from "@/lib/health-presentation";

type Backend = NonNullable<StatusPayload["backends"]>[number];

function engineName(service: Service): string {
  return service.name;
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

/** A kept-off engine's line on the list: when Azizam will check it, or who must act first. */
function keptOffWords(service: Service): string | null {
  const check = service.config.engine_check as
    | { status?: string; reason?: string }
    | undefined;
  if (service.enabled || check?.status !== "not_started") return null;
  if (check.reason === "needkey") return "Waiting for the owner to add a key";
  const lifecycle = (service as { lifecycle?: { driver?: string } }).lifecycle;
  return lifecycle?.driver === "command"
    ? "Not started; checked on first start"
    : "Not checked yet; checked when you turn it on or press Check now";
}

function EnginesContents() {
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const router = useRouter();
  const selectedServiceId = searchParams.get("service");
  const openEngineId = searchParams.get("engine");
  const [notice, setNotice] = useState<string | null>(null);
  const [showNotInUse, setShowNotInUse] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showInfrastructure, setShowInfrastructure] = useState<boolean>();
  const [addOpen, setAddOpen] = useState(false);
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

  function openEngine(serviceId: string) {
    router.push(`/engines/?engine=${encodeURIComponent(serviceId)}`);
  }

  function backToList() {
    router.push("/engines/");
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
    void queryClient.invalidateQueries({ queryKey: queryKeys.status });
  }

  const remove = useMutation({
    mutationFn: (service: Service) => api.services.remove(service.id),
    onSuccess: (registry, service) => {
      updateRegistry(registry);
      setRemoveTarget(null);
      toast.success("Engine removed");
      if (openEngineId === service.id) backToList();
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
    const keptOff = keptOffWords(service);
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
            // The whole two-line row stays one 44 px target, even when a row
            // has no metadata line.
            className="flex min-h-11 w-full min-w-0 flex-col justify-center text-left"
            onClick={() => openEngine(service.id)}
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
            {keptOff ? (
              <p className="text-meta text-muted-foreground mt-0.5 truncate">
                {keptOff}
              </p>
            ) : (alsoServes || checkedAt !== null) && (
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
            <OverflowMenuItem onSelect={() => openEngine(service.id)}>
              Open engine
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

  const removeDialog = (
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
  );

  if (openEngineId) {
    return (
      <>
        <EngineRecordPage
          serviceId={openEngineId}
          notice={notice}
          onDismissNotice={() => setNotice(null)}
          onBack={backToList}
          onOpenEngine={openEngine}
          onRemove={(id) => {
            const target = registryData?.services[id];
            if (target) setRemoveTarget(target);
          }}
        />
        {removeDialog}
      </>
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
                  ? "An engine is a server that runs AI models for Azizam."
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
      <AddEngineDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onAdded={({ serviceId, message, registry }) => {
          updateRegistry(registry);
          setAddOpen(false);
          setNotice(message);
          openEngine(serviceId);
        }}
        onOpenEngine={(serviceId) => {
          setAddOpen(false);
          openEngine(serviceId);
        }}
      />
      {removeDialog}
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
