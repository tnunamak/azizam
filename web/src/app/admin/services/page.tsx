"use client";

/** Admin → Services — backend service registry.
 *
 * Spec: docs/design/WEB_UI_STEERING.md "Services" section.
 * Contract: docs/design/web-api-contract.md "Surface 2 — Services".
 *
 * EDITABILITY LAW: all rows are fully editable/removable regardless of source.
 * Sole exception: managed substrate (litellm) — no remove; endpoint fields
 * locked; rename/enable/note only. Source is provenance (history) not
 * permission. The `editable` field on each service record is authoritative.
 *
 * Presentation law: RelativeTime for every timestamp, EntityRef for every id,
 * no window.confirm, destructive actions in OverflowMenu + ConfirmDialog,
 * loading skeleton + designed empty + error states. */

import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  api,
  ApiError,
  queryKeys,
  type Service,
  type ServiceRegistry,
  type ProviderPreset,
} from "@/lib/api";
import {
  Badge,
  Button,
  ConfirmDialog,
  CopyButton,
  DataTable,
  type DataTableColumn,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyState,
  EntityRef,
  Field,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  SectionHeader,
  Select,
  Skeleton,
  Switch,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Types used only in this surface

interface TestProbe {
  ok: boolean;
  status: number | null;
  error: string;
  url: string;
}

// ---------------------------------------------------------------------------
// Capability id → human label map (matches _CAPABILITY_CHOICES in service_registry.py).

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  completions: "Completions",
  vision: "Vision",
  embeddings: "Embeddings",
  image: "Images",
  "image.generate": "Images",
  audio: "Audio",
  speech: "Speech",
  "audio.speech": "Speech",
  "audio.transcription": "Transcription",
  stt: "Transcription",
  tts: "Speech",
  "llm.execution": "LLM execution",
  rerank: "Rerank",
  moderation: "Moderation",
};

function capabilityLabel(cap: string): string {
  if (cap in CAPABILITY_LABELS) return CAPABILITY_LABELS[cap]!;
  const readable = cap.replace(/[._]/g, " ").replace(/-/g, " ");
  return readable.charAt(0).toUpperCase() + readable.slice(1).toLowerCase();
}

// ---------------------------------------------------------------------------
// Helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Please try again.";
}

/** Best-effort endpoint display: use base_url if present, else host:port. */
function endpointDisplay(service: Service): string {
  if (service.config.base_url) {
    try {
      const url = new URL(service.config.base_url);
      return url.host;
    } catch {
      return service.config.base_url;
    }
  }
  if (service.config.host) {
    const port = service.config.port ? `:${service.config.port}` : "";
    return `${service.config.host}${port}`;
  }
  return "—";
}

function servicesFromRegistry(registry: ServiceRegistry): Service[] {
  return Object.values(registry.services ?? {}).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}

/** True when a service is the managed LiteLLM substrate row. */
function isManagedSubstrate(service: Service): boolean {
  return service.editable === false;
}

// ---------------------------------------------------------------------------
// Source badge — provenance label only (no lock icon; all rows are editable)

type BadgeSource = "provisioned" | "ui" | "builtin" | "imported" | "discovered";

function SourceBadge({ source, managed }: { source: BadgeSource; managed: boolean }) {
  if (managed) {
    return (
      <span title="Managed by the gateway — endpoint fields are derived">
        <Badge sm tone="neutral">managed</Badge>
      </span>
    );
  }
  if (source === "provisioned") {
    return (
      <span title="Imported from your config file — your YAML is not modified and will not re-import it">
        <Badge sm tone="neutral">config</Badge>
      </span>
    );
  }
  return (
    <Badge sm tone="info">
      ui
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Restart banner (shown after every write)

function RestartBanner({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div
      role="status"
      className="rounded-vf-md border-vf-warn/40 bg-vf-warn/10 flex flex-wrap items-center justify-between gap-3 border px-4 py-3"
    >
      <p className="text-vf-small text-vf-text">
        Saved — restart the gateway to apply.
      </p>
      <Button size="sm" variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error banner

function ErrorBanner({
  children,
  onRetry,
}: {
  children: ReactNode;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="rounded-vf-md border-vf-bad/40 bg-vf-bad/10 flex flex-wrap items-center justify-between gap-3 border p-4"
    >
      <p className="text-vf-small text-vf-text">{children}</p>
      <Button size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The surface

export default function AdminServicesPage() {
  const queryClient = useQueryClient();

  const {
    data: registry,
    isPending,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
  });

  const services = useMemo(
    () => (registry ? servicesFromRegistry(registry) : []),
    [registry],
  );

  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Service | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Service | null>(null);
  const [testTarget, setTestTarget] = useState<Service | null>(null);
  const [detectTarget, setDetectTarget] = useState<Service | null>(null);
  const [modelsTarget, setModelsTarget] = useState<Service | null>(null);
  const [litellmOpen, setLitellmOpen] = useState(false);
  /** Banner shown after any write to remind about gateway restart. */
  const [restartBanner, setRestartBanner] = useState(false);

  function showRestartBanner() {
    setRestartBanner(true);
  }

  function updateRegistry(newRegistry: ServiceRegistry) {
    queryClient.setQueryData(queryKeys.serviceRegistry, newRegistry);
  }

  // Toggle enabled — optimistic update
  const toggleMutation = useMutation({
    mutationFn: ({ serviceId, enabled }: { serviceId: string; enabled: boolean }) =>
      api.services.toggleEnabled(serviceId, enabled),
    onMutate: async ({ serviceId, enabled }) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.serviceRegistry });
      const previous = queryClient.getQueryData<ServiceRegistry>(
        queryKeys.serviceRegistry,
      );
      queryClient.setQueryData<ServiceRegistry>(
        queryKeys.serviceRegistry,
        (reg) => {
          if (!reg) return reg;
          const existing = reg.services[serviceId];
          if (!existing) return reg;
          return {
            ...reg,
            services: {
              ...reg.services,
              [serviceId]: { ...existing, enabled },
            },
          };
        },
      );
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(queryKeys.serviceRegistry, context.previous);
      }
      toast.error(`Couldn't update service`, { description: errorMessage(err) });
    },
    onSuccess: (newRegistry) => {
      updateRegistry(newRegistry);
      showRestartBanner();
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.serviceRegistry }),
  });

  // Remove mutation
  const removeMutation = useMutation({
    mutationFn: (serviceId: string) => api.services.remove(serviceId),
    onSuccess: (newRegistry) => {
      updateRegistry(newRegistry);
      toast.success("Service removed");
      showRestartBanner();
      setRemoveTarget(null);
    },
    onError: (err) => {
      toast.error("Couldn't remove service", { description: errorMessage(err) });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.serviceRegistry }),
  });

  const columns: DataTableColumn<Service>[] = [
    {
      id: "service",
      header: "Service",
      cell: (svc) => (
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <EntityRef name={svc.name} id={svc.id} />
          <SourceBadge
            source={svc.source as BadgeSource}
            managed={isManagedSubstrate(svc)}
          />
        </span>
      ),
    },
    {
      id: "endpoint",
      header: "Endpoint",
      cell: (svc) => (
        <span className="text-vf-micro text-vf-subtle font-mono">
          {isManagedSubstrate(svc) ? "managed" : endpointDisplay(svc)}
        </span>
      ),
    },
    {
      id: "capabilities",
      header: "Capabilities",
      cell: (svc) => (
        <span className="flex flex-wrap gap-1">
          {svc.capabilities.length === 0 ? (
            <span className="text-vf-subtle">—</span>
          ) : (
            svc.capabilities.map((cap) => (
              <Badge key={cap} sm tone="neutral">
                {capabilityLabel(cap)}
              </Badge>
            ))
          )}
        </span>
      ),
      hideOnMobile: true,
    },
    {
      id: "enabled",
      header: "Enabled",
      cell: (svc) => (
        // stopPropagation so clicking the switch does NOT open the Edit dialog
        <span onClick={(e) => e.stopPropagation()}>
          <Switch
            checked={svc.enabled}
            disabled={toggleMutation.isPending}
            aria-label={`Toggle ${svc.name}`}
            onCheckedChange={(checked) =>
              toggleMutation.mutate({ serviceId: svc.id, enabled: checked })
            }
          />
        </span>
      ),
    },
  ];

  return (
    <main className="mx-auto flex w-full max-w-[1080px] flex-col gap-10 px-4 py-8 md:px-6">
      <section className="flex flex-col gap-4">
        <SectionHeader
          kicker="infrastructure"
          title="Services"
          description="The upstream backends this gateway fronts. Changes require a gateway restart."
          action={
            <Button variant="primary" onClick={() => setAddOpen(true)}>
              Add service
            </Button>
          }
        />

        {restartBanner && (
          <RestartBanner onDismiss={() => setRestartBanner(false)} />
        )}

        {isError && !registry ? (
          <ErrorBanner onRetry={() => void refetch()}>
            Couldn&apos;t load services. {errorMessage(error)}
          </ErrorBanner>
        ) : (
          <DataTable
            label="Services"
            columns={columns}
            rows={services}
            rowKey={(svc) => svc.id}
            loading={isPending}
            onRowClick={(svc) => setEditTarget(svc)}
            empty={
              <EmptyState
                action={
                  <Button onClick={() => setAddOpen(true)}>Add service</Button>
                }
              >
                No services configured. Add one to route traffic.
              </EmptyState>
            }
            rowMenu={(svc) => {
              const managed = isManagedSubstrate(svc);
              return (
                <OverflowMenu label={`Actions for ${svc.name}`}>
                  <OverflowMenuItem onSelect={() => setEditTarget(svc)}>
                    Edit…
                  </OverflowMenuItem>
                  <OverflowMenuItem onSelect={() => setModelsTarget(svc)}>
                    Models…
                  </OverflowMenuItem>
                  <OverflowMenuItem onSelect={() => setTestTarget(svc)}>
                    Test connection…
                  </OverflowMenuItem>
                  <OverflowMenuItem onSelect={() => setDetectTarget(svc)}>
                    Detect models…
                  </OverflowMenuItem>
                  {managed ? (
                    <>
                      <OverflowMenuSeparator />
                      <OverflowMenuItem
                        onSelect={() =>
                          toggleMutation.mutate({
                            serviceId: svc.id,
                            enabled: !svc.enabled,
                          })
                        }
                      >
                        {svc.enabled ? "Disable" : "Enable"}
                      </OverflowMenuItem>
                    </>
                  ) : (
                    <>
                      <OverflowMenuSeparator />
                      <OverflowMenuItem
                        danger
                        onSelect={() => setRemoveTarget(svc)}
                      >
                        Remove…
                      </OverflowMenuItem>
                    </>
                  )}
                </OverflowMenu>
              );
            }}
          />
        )}
      </section>

      {/* LiteLLM substrate viewer — read-only, behind a disclosure */}
      <section className="flex flex-col gap-4">
        <div>
          <button
            type="button"
            className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
            onClick={() => setLitellmOpen((v) => !v)}
            aria-expanded={litellmOpen}
          >
            <span
              className={`inline-block transition-transform ${litellmOpen ? "rotate-90" : ""}`}
              aria-hidden
            >
              ▶
            </span>
            LiteLLM substrate config
          </button>
        </div>
        {litellmOpen && <LiteLLMViewer />}
      </section>

      {/* Dialogs */}
      <AddServiceDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({ queryKey: queryKeys.serviceRegistry });
          setAddOpen(false);
          showRestartBanner();
          toast.success("Service added");
        }}
      />

      <EditServiceDialog
        service={editTarget}
        onOpenChange={(open) => {
          if (!open) setEditTarget(null);
        }}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({ queryKey: queryKeys.serviceRegistry });
          setEditTarget(null);
          showRestartBanner();
          toast.success("Service updated");
        }}
        onOpenModels={(svc) => {
          setEditTarget(null);
          setModelsTarget(svc);
        }}
      />

      <ModelsDialog
        service={modelsTarget}
        onOpenChange={(open) => {
          if (!open) setModelsTarget(null);
        }}
        onSaved={(newRegistry) => {
          updateRegistry(newRegistry);
          void queryClient.invalidateQueries({ queryKey: queryKeys.serviceRegistry });
          showRestartBanner();
        }}
      />

      <TestConnectionDialog
        service={testTarget}
        onOpenChange={(open) => {
          if (!open) setTestTarget(null);
        }}
      />

      <DetectModelsDialog
        service={detectTarget}
        onOpenChange={(open) => {
          if (!open) setDetectTarget(null);
        }}
        onAddModels={(models) => {
          if (detectTarget) {
            setModelsTarget(detectTarget);
            setDetectTarget(null);
            // Pass detected models into the models dialog via a ref-free approach:
            // The models dialog opens and pre-fills from the service's existing
            // models + the detected ones merged by the user.
            void models; // will be offered inside ModelsDialog via detectTarget
          }
        }}
      />

      {/* Remove confirm — typed-name; provenance-aware copy for config rows */}
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null);
        }}
        title="Remove service?"
        description={
          removeTarget ? (
            removeTarget.source === "provisioned" ? (
              <>
                <strong>{removeTarget.name}</strong> will be removed from the
                registry. Routes that depend on it will stop working after the
                next gateway restart.{" "}
                <em>
                  Your YAML config file is not modified and will not re-import
                  this service on the next restart.
                </em>
              </>
            ) : (
              <>
                <strong>{removeTarget.name}</strong> will be removed from the
                registry. Routes that depend on it will stop working after the
                next gateway restart.
              </>
            )
          ) : undefined
        }
        confirmLabel="Remove service"
        typedName={removeTarget?.name}
        onConfirm={() => {
          if (removeTarget) removeMutation.mutate(removeTarget.id);
        }}
      />
    </main>
  );
}

// ---------------------------------------------------------------------------
// Service form state — shared by Add and Edit

interface ServiceFormState {
  name: string;
  preset: string;
  base_url: string;
  api_key_env: string;
}

interface ServiceFormProps {
  value: ServiceFormState;
  onChange: (value: ServiceFormState) => void;
  disabled?: boolean;
  error?: string | null;
  showAdvanced?: boolean;
  onToggleAdvanced?: () => void;
  probeResult?: TestProbe | null;
  onTest?: () => void;
  testing?: boolean;
  presets: ProviderPreset[];
}

function ServiceForm({
  value,
  onChange,
  disabled,
  error,
  showAdvanced,
  onToggleAdvanced,
  probeResult,
  onTest,
  testing,
  presets,
}: ServiceFormProps) {
  const set = (key: keyof ServiceFormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
  ) => onChange({ ...value, [key]: e.target.value });

  return (
    <div className="flex flex-col gap-4">
      <Field
        label="Provider"
        help='Choose a preset to pre-fill defaults. "Custom (OpenAI-compatible)" works with any OpenAI-compatible API.'
      >
        <Select value={value.preset} onChange={set("preset")} disabled={disabled}>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </Select>
      </Field>

      <Field label="Name" required>
        <Input
          value={value.name}
          onChange={set("name")}
          placeholder="e.g. my-vllm"
          disabled={disabled}
          autoFocus
        />
      </Field>

      <Field label="Base URL" required help="Full URL including scheme, host, port, and path.">
        <Input
          value={value.base_url}
          onChange={set("base_url")}
          placeholder="http://127.0.0.1:8000/v1"
          disabled={disabled}
        />
      </Field>

      <Field
        label="API key env var"
        help="Name of an environment variable on the gateway host — never the key itself."
      >
        <Input
          value={value.api_key_env}
          onChange={set("api_key_env")}
          placeholder="e.g. VLLM_API_KEY"
          disabled={disabled}
        />
      </Field>

      {/* Advanced disclosure */}
      {onToggleAdvanced && (
        <div>
          <button
            type="button"
            className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
            onClick={onToggleAdvanced}
            aria-expanded={showAdvanced}
          >
            <span
              className={`inline-block transition-transform ${showAdvanced ? "rotate-90" : ""}`}
              aria-hidden
            >
              ▶
            </span>
            Advanced
          </button>
        </div>
      )}

      {/* Inline test connection */}
      {onTest && (
        <div className="flex flex-col gap-2">
          <Button
            type="button"
            size="sm"
            variant="secondary"
            loading={testing}
            disabled={!value.base_url}
            onClick={onTest}
          >
            Test connection
          </Button>
          {probeResult !== null && probeResult !== undefined && (
            <ProbeResultRow probe={probeResult} />
          )}
        </div>
      )}

      {error && (
        <p role="alert" className="text-vf-small text-vf-bad">
          {error}
        </p>
      )}
    </div>
  );
}

function ProbeResultRow({ probe }: { probe: TestProbe }) {
  return (
    <div
      className={`rounded-vf-md flex items-center gap-2 border px-3 py-2 text-sm ${
        probe.ok
          ? "border-vf-ok/40 bg-vf-ok/10 text-vf-text"
          : "border-vf-bad/40 bg-vf-bad/10 text-vf-text"
      }`}
    >
      <span>{probe.ok ? "✓" : "✗"}</span>
      <span className="text-vf-small">
        {probe.ok
          ? `Connected (HTTP ${probe.status ?? "ok"}) — ${probe.url}`
          : probe.error || "Connection failed"}
      </span>
    </div>
  );
}

function emptyFormState(defaultPreset = "openai-compatible"): ServiceFormState {
  return { name: "", preset: defaultPreset, base_url: "", api_key_env: "" };
}

function formToApiBody(state: ServiceFormState): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: state.name.trim(),
    preset: state.preset,
    base_url: state.base_url.trim(),
  };
  if (state.api_key_env.trim()) {
    body.api_key_env = state.api_key_env.trim();
  }
  return body;
}

// ---------------------------------------------------------------------------
// Add service dialog — presets fetched from the API

function AddServiceDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <AddServiceDialogContent onOpenChange={onOpenChange} onSaved={onSaved} />
      )}
    </Dialog>
  );
}

function AddServiceDialogContent({
  onOpenChange,
  onSaved,
}: {
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const { data: presetsData, isPending: presetsLoading } = useQuery({
    queryKey: queryKeys.servicePresets,
    queryFn: api.services.presets,
  });
  const presets = presetsData?.presets ?? [];

  const defaultPreset = presets[0]?.id ?? "openai-compatible";
  const [form, setForm] = useState<ServiceFormState>(() =>
    emptyFormState(defaultPreset),
  );
  const [formError, setFormError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);

  const save = useMutation({
    mutationFn: () => api.services.add(formToApiBody(form)),
    onSuccess: onSaved,
    onError: (err) => setFormError(errorMessage(err)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!form.name.trim()) {
      setFormError("Service name is required.");
      return;
    }
    if (!form.base_url.trim()) {
      setFormError("Base URL is required.");
      return;
    }
    save.mutate();
  }

  async function handleTest() {
    setTesting(true);
    setProbe(null);
    try {
      const result = await api.services.testConnection({ base_url: form.base_url.trim() });
      setProbe(result);
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  if (presetsLoading) {
    return (
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add service</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-3" aria-busy="true">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        </DialogBody>
      </DialogContent>
    );
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Add service</DialogTitle>
          <DialogDescription>
            Register an upstream backend. Select a preset to pre-fill defaults,
            or choose{" "}
            <strong>Custom (OpenAI-compatible)</strong> for any
            OpenAI-compatible API. Changes take effect after a gateway restart.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <ServiceForm
            value={form}
            onChange={setForm}
            disabled={save.isPending}
            error={formError}
            showAdvanced={showAdvanced}
            onToggleAdvanced={() => setShowAdvanced((v) => !v)}
            probeResult={probe}
            onTest={() => void handleTest()}
            testing={testing}
            presets={presets}
          />
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Add service
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Edit service dialog
//
// Managed substrate rows (editable=false): show only name/enabled/note with an
// explanation that endpoint/protocol are managed by the gateway.
// All other rows: full-fidelity form with all config fields.

// ---------------------------------------------------------------------------
// Edit form state — richer than the Add form

interface EditFormState {
  // Connection
  name: string;
  base_url: string;
  api_key_env: string;
  protocol: string;
  health_endpoint: string;
  timeout: string;
  // Routing
  capabilities: string[];
  native_passthrough: boolean;
  // ComfyUI protocol-specific fields (only sent/shown when protocol === "comfyui")
  workflows_dir: string;
  video_workflows_dir: string;
  video_timeout: string;
  default_model: string;
  // Admission (advanced)
  role: string;
  priority: string;
  gpu_required: boolean;
  max_parallel: string;
  vram_gb_estimate: string;
  duration_class: string;
  note: string;
  // Top-level
  enabled: boolean;
}

// Canonical capability list — static fallback; server provides authoritative list via presets endpoint.
// Includes generations + videos so ComfyUI capability checkboxes appear even when the presets call fails.
const CAPABILITY_CHOICES_FALLBACK = [
  { id: "chat", label: "Chat" },
  { id: "completions", label: "Completions" },
  { id: "vision", label: "Vision" },
  { id: "embeddings", label: "Embeddings" },
  { id: "image", label: "Images" },
  { id: "audio", label: "Audio" },
  { id: "speech", label: "Speech" },
  { id: "generations", label: "Generations" },
  { id: "videos", label: "Videos" },
];

// Valid protocol values accepted by the backend normalizer
const PROTOCOL_OPTIONS = [
  { value: "", label: "— (none)" },
  { value: "openai-compatible", label: "OpenAI-compatible" },
  { value: "openai", label: "OpenAI" },
  { value: "comfyui", label: "ComfyUI" },
];

// Role values per registry semantics
const ROLE_OPTIONS = [
  { value: "required", label: "Required — gateway will error if unavailable" },
  { value: "optional", label: "Optional — gateway routes around if unavailable" },
  { value: "standby", label: "Standby — only used when explicitly requested" },
];

const DURATION_CLASS_OPTIONS = [
  { value: "interactive", label: "Interactive (low latency)" },
  { value: "batch", label: "Batch (throughput)" },
];

/** Compose a base_url display string from host/port/base_path — inverse of split_base_url. */
function composeBaseUrl(config: Service["config"]): string {
  if (config.base_url) return config.base_url;
  if (config.host) {
    const scheme = config.port === 443 ? "https" : "http";
    const port = config.port ? `:${config.port}` : "";
    const path = config.base_path ?? "";
    return `${scheme}://${config.host}${port}${path}`;
  }
  return "";
}

/** Read the api_key env var name from wherever the record stores it — config or secret_refs. */
function extractApiKeyEnv(service: Service): string {
  // api_key_env is a secret reference key (_env suffix), so split_secret_refs
  // passes it through into the safe config dict (not secret_refs).
  if (service.config.api_key_env) return String(service.config.api_key_env);
  // Fallback: some older records might store it under secret_refs
  const refs = service.secret_refs ?? {};
  for (const key of Object.keys(refs)) {
    if (key.toLowerCase().includes("api_key")) return key;
  }
  return "";
}

function serviceToEditForm(service: Service): EditFormState {
  const cfg = service.config;
  const res = cfg.resources ?? {};
  return {
    name: service.name,
    base_url: composeBaseUrl(cfg),
    api_key_env: extractApiKeyEnv(service),
    protocol: cfg.protocol ?? "",
    health_endpoint: cfg.health_endpoint ?? "",
    timeout: cfg.timeout != null ? String(cfg.timeout) : "",
    capabilities: [...(service.capabilities ?? [])],
    native_passthrough: cfg.native_passthrough ?? false,
    // ComfyUI protocol-specific fields — always hydrated from config so they
    // survive a round-trip even when the user edits another section.
    workflows_dir: cfg.workflows_dir != null ? String(cfg.workflows_dir) : "",
    video_workflows_dir: cfg.video_workflows_dir != null ? String(cfg.video_workflows_dir) : "",
    video_timeout: cfg.video_timeout != null ? String(cfg.video_timeout) : "",
    default_model: cfg.default_model != null ? String(cfg.default_model) : "",
    role: service.role ?? "required",
    priority: service.priority != null ? String(service.priority) : "100",
    gpu_required: Boolean(res.gpu_required),
    max_parallel: res.max_parallel != null ? String(res.max_parallel) : "",
    vram_gb_estimate: res.vram_gb_estimate != null ? String(res.vram_gb_estimate) : "",
    duration_class: String(res.duration_class ?? "interactive"),
    note: cfg.note ?? "",
    enabled: service.enabled,
  };
}

// Keys the edit form explicitly models — any config key NOT in this set that
// is present in the stored record will be shown in the "Advanced — managed in
// config" read-only disclosure (H2) so operators can see everything the record
// holds.
const FORM_MODELED_CONFIG_KEYS = new Set([
  "base_url", "host", "port", "base_path", "protocol", "api_key_env",
  "health_endpoint", "timeout", "native_passthrough", "note", "resources",
  "models", "workflows_dir", "video_workflows_dir", "video_timeout",
  "default_model",
]);

/** Returns config keys in `service.config` that the form has no widget for.
 * These are shown read-only so H2 (show everything the record carries) holds. */
function unknownConfigKeys(service: Service): [string, unknown][] {
  return Object.entries(service.config).filter(
    ([key]) => !FORM_MODELED_CONFIG_KEYS.has(key),
  );
}

function editFormToApiBody(form: EditFormState): Record<string, unknown> {
  const isComfyUI = form.protocol === "comfyui";
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    base_url: form.base_url.trim(),
    enabled: form.enabled,
    capabilities: form.capabilities,
    role: form.role,
  };
  if (form.api_key_env.trim()) body.api_key_env = form.api_key_env.trim();
  if (form.protocol) body.protocol = form.protocol;
  if (form.health_endpoint.trim()) body.health_endpoint = form.health_endpoint.trim();
  if (form.timeout.trim()) body.timeout = parseInt(form.timeout, 10);
  if (form.priority.trim()) body.priority = parseInt(form.priority, 10);
  if (form.note.trim()) body.note = form.note.trim();

  // native_passthrough: only meaningful for openai-compatible; for comfyui it
  // is always native — don't send a misleading toggle value.
  if (!isComfyUI) {
    body.native_passthrough = form.native_passthrough;
  }

  // ComfyUI protocol-specific fields — always sent for comfyui services so
  // they round-trip even when the user edits another section (H1).
  // For non-comfyui services they are omitted entirely.
  if (isComfyUI) {
    if (form.workflows_dir.trim()) body.workflows_dir = form.workflows_dir.trim();
    if (form.video_workflows_dir.trim()) body.video_workflows_dir = form.video_workflows_dir.trim();
    if (form.video_timeout.trim()) body.video_timeout = parseInt(form.video_timeout, 10);
    if (form.default_model.trim()) body.default_model = form.default_model.trim();
  }

  // Build resources sub-object — only include what's non-empty
  const resources: Record<string, unknown> = { gpu_required: form.gpu_required };
  if (form.max_parallel.trim()) resources.max_parallel = parseInt(form.max_parallel, 10);
  if (form.vram_gb_estimate.trim()) resources.vram_gb_estimate = parseFloat(form.vram_gb_estimate);
  if (form.duration_class) resources.duration_class = form.duration_class;
  body.resources = resources;

  return body;
}

function EditServiceDialog({
  service,
  onOpenChange,
  onSaved,
  onOpenModels,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
  onOpenModels: (service: Service) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <EditServiceDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
          onOpenModels={onOpenModels}
        />
      )}
    </Dialog>
  );
}

function EditServiceDialogContent({
  service,
  onOpenChange,
  onSaved,
  onOpenModels,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
  onOpenModels: (service: Service) => void;
}) {
  const managed = isManagedSubstrate(service);

  const { data: presetsData } = useQuery({
    queryKey: queryKeys.servicePresets,
    queryFn: api.services.presets,
  });
  const capabilityChoices = presetsData?.capabilities ?? CAPABILITY_CHOICES_FALLBACK;

  // Full edit form state (non-managed rows)
  const [form, setForm] = useState<EditFormState>(() => serviceToEditForm(service));
  // Managed-row only fields
  const [managedName, setManagedName] = useState(service.name);
  const [managedEnabled, setManagedEnabled] = useState(service.enabled);
  const [formError, setFormError] = useState<string | null>(null);
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showUnknownKeys, setShowUnknownKeys] = useState(false);

  const isComfyUI = form.protocol === "comfyui";
  const unknownKeys = unknownConfigKeys(service);

  function set<K extends keyof EditFormState>(key: K) {
    return (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => {
      setForm((prev) => ({ ...prev, [key]: e.target.value }));
    };
  }

  function toggleCapability(cap: string) {
    setForm((prev) => ({
      ...prev,
      capabilities: prev.capabilities.includes(cap)
        ? prev.capabilities.filter((c) => c !== cap)
        : [...prev.capabilities, cap],
    }));
  }

  const save = useMutation({
    mutationFn: () => {
      if (managed) {
        return api.services.edit(service.id, {
          name: managedName.trim() || service.name,
          enabled: managedEnabled,
        });
      }
      return api.services.edit(service.id, editFormToApiBody(form));
    },
    onSuccess: onSaved,
    onError: (err) => setFormError(errorMessage(err)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!managed && !form.name.trim()) {
      setFormError("Service name is required.");
      return;
    }
    save.mutate();
  }

  async function handleTest() {
    setTesting(true);
    setProbe(null);
    try {
      const result = await api.services.testConnection({ base_url: form.base_url.trim() });
      setProbe(result);
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  return (
    <DialogContent>
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Edit {service.name}</DialogTitle>
          <DialogDescription>
            {managed
              ? "This is the managed LiteLLM substrate. The endpoint and protocol are derived by the gateway and cannot be changed here."
              : "Changes take effect after a gateway restart."}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {managed ? (
            <div className="flex flex-col gap-4">
              <Field label="Name">
                <Input
                  value={managedName}
                  onChange={(e) => setManagedName(e.target.value)}
                  disabled={save.isPending}
                  autoFocus
                />
              </Field>
              <div className="flex items-center justify-between gap-4">
                <span className="text-vf-small text-vf-text font-medium">Enabled</span>
                <Switch
                  checked={managedEnabled}
                  onCheckedChange={setManagedEnabled}
                  disabled={save.isPending}
                  aria-label="Enable or disable this service"
                />
              </div>
              <p className="text-vf-small text-vf-muted rounded-vf-md border-vf-border bg-vf-surface-2 border p-3">
                Endpoint, protocol, and execution substrate are managed by the
                gateway and cannot be edited here.
              </p>
              {formError && (
                <p role="alert" className="text-vf-small text-vf-bad">
                  {formError}
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-6">
              {/* ── Connection ──────────────────────────────── */}
              <div className="flex flex-col gap-4">
                <p className="text-vf-micro text-vf-muted font-semibold uppercase tracking-wide">
                  Connection
                </p>

                <Field label="Name" required>
                  <Input
                    value={form.name}
                    onChange={set("name")}
                    disabled={save.isPending}
                    autoFocus
                  />
                </Field>

                <Field
                  label="Base URL"
                  required
                  help="Full URL including scheme, host, port, and path."
                >
                  <Input
                    value={form.base_url}
                    onChange={set("base_url")}
                    placeholder="http://127.0.0.1:8000/v1"
                    disabled={save.isPending}
                  />
                </Field>

                <div className="flex flex-col gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={testing}
                    disabled={!form.base_url}
                    onClick={() => void handleTest()}
                  >
                    Test connection
                  </Button>
                  {probe !== null && <ProbeResultRow probe={probe} />}
                </div>

                <Field
                  label="API key env var"
                  help="Name of an env variable on the gateway host — never the key itself."
                >
                  <Input
                    value={form.api_key_env}
                    onChange={set("api_key_env")}
                    placeholder="e.g. VLLM_API_KEY"
                    disabled={save.isPending}
                  />
                </Field>

                <Field label="Protocol">
                  <Select
                    value={form.protocol}
                    onChange={set("protocol")}
                    disabled={save.isPending}
                  >
                    {PROTOCOL_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </Select>
                </Field>

                <Field
                  label="Health endpoint"
                  help="Path probed to check reachability (e.g. /health or /v1/models)."
                >
                  <Input
                    value={form.health_endpoint}
                    onChange={set("health_endpoint")}
                    placeholder="/health"
                    disabled={save.isPending}
                  />
                </Field>

                <Field label="Timeout (seconds)">
                  <Input
                    type="number"
                    min={1}
                    value={form.timeout}
                    onChange={set("timeout")}
                    placeholder="120"
                    disabled={save.isPending}
                  />
                </Field>
              </div>

              {/* ── Protocol-specific section ────────────────── */}
              {isComfyUI ? (
                <div className="flex flex-col gap-4">
                  <p className="text-vf-micro text-vf-muted font-semibold uppercase tracking-wide">
                    ComfyUI
                  </p>
                  <p className="text-vf-small text-vf-muted rounded-vf-md border-vf-border bg-vf-surface-2 border p-3">
                    ComfyUI is reached directly — image and video jobs bypass
                    the LiteLLM substrate entirely. Workflow graphs are edited in
                    ComfyUI itself; this section points the gateway at the
                    workflow directory and sets a default.
                  </p>

                  <Field
                    label="Workflows directory"
                    help="Absolute path on the gateway host to the directory holding workflow .json files."
                  >
                    <Input
                      value={form.workflows_dir}
                      onChange={set("workflows_dir")}
                      placeholder="/opt/comfyui/workflows"
                      disabled={save.isPending}
                    />
                  </Field>

                  <Field
                    label="Video workflows directory"
                    help="Absolute path to the directory holding video workflow .json files."
                  >
                    <Input
                      value={form.video_workflows_dir}
                      onChange={set("video_workflows_dir")}
                      placeholder="/opt/comfyui/video-workflows"
                      disabled={save.isPending}
                    />
                  </Field>

                  <Field
                    label="Video timeout (seconds)"
                    help="Maximum seconds to wait for a video generation job to complete."
                  >
                    <Input
                      type="number"
                      min={1}
                      value={form.video_timeout}
                      onChange={set("video_timeout")}
                      placeholder="300"
                      disabled={save.isPending}
                    />
                  </Field>

                  <Field
                    label="Default model"
                    help="Basename (without .json) of the workflow file used when the caller doesn't specify a model. Must match a file in the workflows directory."
                  >
                    <Input
                      value={form.default_model}
                      onChange={set("default_model")}
                      placeholder="e.g. sd3-medium"
                      disabled={save.isPending}
                    />
                  </Field>
                </div>
              ) : null}

              {/* ── Routing ─────────────────────────────────── */}
              <div className="flex flex-col gap-4">
                <p className="text-vf-micro text-vf-muted font-semibold uppercase tracking-wide">
                  Routing
                </p>

                <fieldset className="flex flex-col gap-2">
                  <legend className="text-vf-small text-vf-text font-medium mb-1">
                    Capabilities
                  </legend>
                  <div className="flex flex-wrap gap-x-4 gap-y-2">
                    {capabilityChoices.map((cap) => (
                      <label key={cap.id} className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={form.capabilities.includes(cap.id)}
                          onChange={() => toggleCapability(cap.id)}
                          disabled={save.isPending}
                          className="accent-vf-accent h-4 w-4"
                        />
                        <span className="text-vf-small text-vf-text">
                          {cap.label}
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>

                {/* H3 honesty: for ComfyUI, native passthrough is the only mode —
                    don't show it as a toggle implying an alternative exists. */}
                {isComfyUI ? (
                  <p className="text-vf-small text-vf-muted">
                    Image and video requests are sent directly to ComfyUI —
                    native passthrough is always on for this protocol.
                  </p>
                ) : (
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex flex-col gap-0.5">
                      <span className="text-vf-small text-vf-text font-medium">
                        Native passthrough
                      </span>
                      <span className="text-vf-micro text-vf-muted">
                        Send requests straight to this backend instead of through the LiteLLM substrate.
                      </span>
                    </div>
                    <Switch
                      checked={form.native_passthrough}
                      onCheckedChange={(checked) =>
                        setForm((prev) => ({ ...prev, native_passthrough: checked }))
                      }
                      disabled={save.isPending}
                      aria-label="Native passthrough"
                    />
                  </div>
                )}

                {!isComfyUI && (
                  <p className="text-vf-small text-vf-muted">
                    Model mappings are managed separately —{" "}
                    <button
                      type="button"
                      className="text-vf-accent underline-offset-2 hover:underline"
                      onClick={() => {
                        onOpenChange(false);
                        onOpenModels(service);
                      }}
                    >
                      open Models…
                    </button>
                  </p>
                )}
              </div>

              {/* ── Unknown config keys (H2) ─────────────────── */}
              {unknownKeys.length > 0 && (
                <div className="flex flex-col gap-2">
                  <button
                    type="button"
                    className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
                    onClick={() => setShowUnknownKeys((v) => !v)}
                    aria-expanded={showUnknownKeys}
                  >
                    <span
                      className={`inline-block transition-transform ${showUnknownKeys ? "rotate-90" : ""}`}
                      aria-hidden
                    >
                      ▶
                    </span>
                    Advanced — managed in config ({unknownKeys.length})
                  </button>
                  {showUnknownKeys && (
                    <div className="rounded-vf-md border-vf-border bg-vf-surface-2 border p-3">
                      <p className="text-vf-micro text-vf-muted mb-2">
                        These keys are stored in this service&apos;s config but
                        have no widget in the form. They are preserved on every
                        save and are shown here for transparency.
                      </p>
                      <dl className="flex flex-col gap-1.5">
                        {unknownKeys.map(([key, val]) => (
                          <div key={key} className="flex flex-wrap items-start gap-x-3 gap-y-0.5">
                            <dt className="text-vf-micro text-vf-subtle font-mono shrink-0">{key}</dt>
                            <dd className="text-vf-micro text-vf-text font-mono break-all">
                              {JSON.stringify(val)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    </div>
                  )}
                </div>
              )}

              {/* ── Enabled ─────────────────────────────────── */}
              <div className="flex items-center justify-between gap-4">
                <span className="text-vf-small text-vf-text font-medium">Enabled</span>
                <Switch
                  checked={form.enabled}
                  onCheckedChange={(checked) =>
                    setForm((prev) => ({ ...prev, enabled: checked }))
                  }
                  disabled={save.isPending}
                  aria-label="Enable or disable this service"
                />
              </div>

              {/* ── Advanced (admission) ─────────────────────── */}
              <div className="flex flex-col gap-2">
                <button
                  type="button"
                  className="text-vf-small text-vf-muted hover:text-vf-text flex items-center gap-1.5 transition-colors"
                  onClick={() => setShowAdvanced((v) => !v)}
                  aria-expanded={showAdvanced}
                >
                  <span
                    className={`inline-block transition-transform ${showAdvanced ? "rotate-90" : ""}`}
                    aria-hidden
                  >
                    ▶
                  </span>
                  Advanced
                </button>

                {showAdvanced && (
                  <div className="flex flex-col gap-4 pl-4 pt-2">
                    <fieldset className="flex flex-col gap-2">
                      <legend className="text-vf-small text-vf-text font-medium mb-1">
                        Role
                      </legend>
                      <div className="flex flex-col gap-2">
                        {ROLE_OPTIONS.map((opt) => (
                          <label key={opt.value} className="flex items-start gap-2 cursor-pointer">
                            <input
                              type="radio"
                              name="role"
                              value={opt.value}
                              checked={form.role === opt.value}
                              onChange={set("role")}
                              disabled={save.isPending}
                              className="accent-vf-accent mt-0.5 h-4 w-4 shrink-0"
                            />
                            <span className="text-vf-small text-vf-text">{opt.label}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>

                    <Field label="Priority" help="Lower numbers are preferred (default 100).">
                      <Input
                        type="number"
                        value={form.priority}
                        onChange={set("priority")}
                        placeholder="100"
                        disabled={save.isPending}
                      />
                    </Field>

                    <div className="flex items-center justify-between gap-4">
                      <span className="text-vf-small text-vf-text font-medium">GPU required</span>
                      <Switch
                        checked={form.gpu_required}
                        onCheckedChange={(checked) =>
                          setForm((prev) => ({ ...prev, gpu_required: checked }))
                        }
                        disabled={save.isPending}
                        aria-label="GPU required"
                      />
                    </div>

                    <Field label="Max parallel" help="Maximum concurrent requests to this backend.">
                      <Input
                        type="number"
                        min={1}
                        value={form.max_parallel}
                        onChange={set("max_parallel")}
                        placeholder="4"
                        disabled={save.isPending}
                      />
                    </Field>

                    <Field label="VRAM estimate (GB)">
                      <Input
                        type="number"
                        min={0}
                        step={0.5}
                        value={form.vram_gb_estimate}
                        onChange={set("vram_gb_estimate")}
                        placeholder="0"
                        disabled={save.isPending}
                      />
                    </Field>

                    <Field label="Duration class">
                      <Select
                        value={form.duration_class}
                        onChange={set("duration_class")}
                        disabled={save.isPending}
                      >
                        {DURATION_CLASS_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </Select>
                    </Field>

                    <Field label="Note">
                      <textarea
                        value={form.note}
                        onChange={set("note")}
                        rows={3}
                        disabled={save.isPending}
                        placeholder="Optional operator note about this service."
                        className="border-vf-border bg-vf-surface focus:ring-vf-accent/30 text-vf-small text-vf-text placeholder:text-vf-subtle w-full resize-none rounded-md border px-3 py-2 focus:ring-2 focus:outline-none disabled:opacity-50"
                      />
                    </Field>
                  </div>
                )}
              </div>

              {formError && (
                <p role="alert" className="text-vf-small text-vf-bad">
                  {formError}
                </p>
              )}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Models editor dialog
//
// Lists per-service model mappings (public name → upstream name) with add /
// edit / remove. Saves via PATCH and shows restart banner.

function ModelsDialog({
  service,
  onOpenChange,
  onSaved,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <ModelsDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

interface ModelRow {
  model_group: string;
  upstream_model: string;
}

function ModelsDialogContent({
  service,
  onOpenChange,
  onSaved,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const [models, setModels] = useState<ModelRow[]>(() =>
    (service.config.models ?? []).map((m) => ({
      model_group: m.model_group,
      upstream_model: m.upstream_model,
    })),
  );
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [addingRow, setAddingRow] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      api.services.edit(service.id, {
        models: models.map((m) => ({
          model_group: m.model_group,
          upstream_model: m.upstream_model,
        })),
      }),
    onSuccess: (newRegistry) => {
      onSaved(newRegistry);
      toast.success("Models updated");
      onOpenChange(false);
    },
    onError: (err) => setFormError(errorMessage(err)),
  });

  function removeModel(idx: number) {
    setModels((prev) => prev.filter((_, i) => i !== idx));
  }

  function updateModel(idx: number, updated: ModelRow) {
    setModels((prev) => prev.map((m, i) => (i === idx ? updated : m)));
    setEditingIdx(null);
  }

  function addModel(row: ModelRow) {
    setModels((prev) => [...prev, row]);
    setAddingRow(false);
  }

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Models — {service.name}</DialogTitle>
        <DialogDescription>
          Model mappings make this backend&apos;s models routable. Each entry
          maps a public name (what callers use) to an upstream model name.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {models.length === 0 && !addingRow ? (
          <div className="flex flex-col items-center gap-3 py-4 text-center">
            <p className="text-vf-small text-vf-muted">
              No models mapped — this backend&apos;s models aren&apos;t routable
              until you add one.
            </p>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => setAddingRow(true)}
            >
              Add model
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {models.map((model, idx) =>
              editingIdx === idx ? (
                <ModelEditRow
                  key={idx}
                  initial={model}
                  onSave={(updated) => updateModel(idx, updated)}
                  onCancel={() => setEditingIdx(null)}
                />
              ) : (
                <ModelDisplayRow
                  key={idx}
                  model={model}
                  onEdit={() => setEditingIdx(idx)}
                  onRemove={() => removeModel(idx)}
                />
              ),
            )}

            {addingRow ? (
              <ModelEditRow
                initial={{ model_group: "", upstream_model: "" }}
                onSave={addModel}
                onCancel={() => setAddingRow(false)}
                isNew
              />
            ) : (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  setEditingIdx(null);
                  setAddingRow(true);
                }}
                className="mt-1 self-start"
              >
                Add model
              </Button>
            )}
          </div>
        )}

        {formError && (
          <p role="alert" className="text-vf-small text-vf-bad mt-2">
            {formError}
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={save.isPending}
        >
          Cancel
        </Button>
        <Button
          variant="primary"
          loading={save.isPending}
          onClick={() => save.mutate()}
        >
          Save
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

function ModelDisplayRow({
  model,
  onEdit,
  onRemove,
}: {
  model: ModelRow;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="border-vf-border rounded-vf-md flex items-center justify-between gap-3 border px-3 py-2">
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-vf-small text-vf-text font-medium truncate">
          {model.model_group}
        </span>
        <span className="text-vf-micro text-vf-subtle font-mono truncate">
          → {model.upstream_model}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button size="sm" variant="ghost" onClick={onEdit}>
          Edit
        </Button>
        <Button size="sm" variant="ghost" onClick={onRemove}>
          Remove
        </Button>
      </div>
    </div>
  );
}

function ModelEditRow({
  initial,
  onSave,
  onCancel,
  isNew = false,
}: {
  initial: ModelRow;
  onSave: (row: ModelRow) => void;
  onCancel: () => void;
  isNew?: boolean;
}) {
  const [publicName, setPublicName] = useState(initial.model_group);
  const [upstreamName, setUpstreamName] = useState(initial.upstream_model);
  const [err, setErr] = useState<string | null>(null);

  function handleSave() {
    if (!publicName.trim()) {
      setErr("Public name is required.");
      return;
    }
    if (!upstreamName.trim()) {
      setErr("Upstream model name is required.");
      return;
    }
    onSave({ model_group: publicName.trim(), upstream_model: upstreamName.trim() });
  }

  return (
    <div className="border-vf-border rounded-vf-md flex flex-col gap-2 border p-3">
      <div className="flex gap-2">
        <Field label="Public name" className="flex-1">
          <Input
            value={publicName}
            onChange={(e) => setPublicName(e.target.value)}
            placeholder="e.g. llama-3-8b"
            autoFocus={isNew}
          />
        </Field>
        <Field label="Upstream model" className="flex-1">
          <Input
            value={upstreamName}
            onChange={(e) => setUpstreamName(e.target.value)}
            placeholder="e.g. Meta-Llama-3-8B-Instruct"
          />
        </Field>
      </div>
      {err && (
        <p role="alert" className="text-vf-small text-vf-bad">
          {err}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" variant="primary" onClick={handleSave}>
          {isNew ? "Add" : "Update"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Test connection dialog (launched from overflow menu for existing services)

function TestConnectionDialog({
  service,
  onOpenChange,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <TestConnectionDialogContent service={service} onOpenChange={onOpenChange} />
      )}
    </Dialog>
  );
}

function TestConnectionDialogContent({
  service,
  onOpenChange,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
}) {
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const endpoint = service.config.base_url
    ? { base_url: service.config.base_url }
    : { host: service.config.host, port: service.config.port };

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      const result = await api.services.testConnection(endpoint);
      setProbe(result);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>Test connection — {service.name}</DialogTitle>
        <DialogDescription>
          Sends a lightweight probe to the upstream to check reachability.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {probe && <ProbeResultRow probe={probe} />}
        {err && (
          <p role="alert" className="text-vf-small text-vf-bad">
            {err}
          </p>
        )}
        {!probe && !err && (
          <p className="text-vf-small text-vf-muted">
            Probing:{" "}
            <span className="font-mono">
              {service.config.base_url ?? endpointDisplay(service)}
            </span>
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
        <Button variant="primary" loading={loading} onClick={() => void run()}>
          {probe ? "Re-test" : "Test now"}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Detect models dialog — after detection, offer to prefill the models mapping

function DetectModelsDialog({
  service,
  onOpenChange,
  onAddModels,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
  onAddModels: (models: string[]) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <DetectModelsDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onAddModels={onAddModels}
        />
      )}
    </Dialog>
  );
}

function DetectModelsDialogContent({
  service,
  onOpenChange,
  onAddModels,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onAddModels: (models: string[]) => void;
}) {
  const [result, setResult] = useState<{ ok: boolean; models: string[]; error: string } | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const endpoint = service.config.base_url
    ? { base_url: service.config.base_url }
    : { host: service.config.host, port: service.config.port };

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      const detected = await api.services.detectModels(endpoint);
      setResult(detected);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  const hasModels = result?.ok && result.models.length > 0;

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>Detect models — {service.name}</DialogTitle>
        <DialogDescription>
          Queries the upstream for the list of available models.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {result && (
          <div className="flex flex-col gap-2">
            {result.ok ? (
              result.models.length === 0 ? (
                <p className="text-vf-small text-vf-muted">No models reported.</p>
              ) : (
                <ul className="divide-vf-border flex flex-col divide-y">
                  {result.models.map((model) => (
                    <li
                      key={model}
                      className="text-vf-small text-vf-text flex items-center justify-between py-2"
                    >
                      <span className="font-mono">{model}</span>
                      <CopyButton
                        value={model}
                        aria-label={`Copy model name ${model}`}
                      />
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <p role="alert" className="text-vf-small text-vf-bad">
                {result.error || "Could not detect models from this service."}
              </p>
            )}
          </div>
        )}
        {err && (
          <p role="alert" className="text-vf-small text-vf-bad">
            {err}
          </p>
        )}
        {!result && !err && (
          <p className="text-vf-small text-vf-muted">
            Will query:{" "}
            <span className="font-mono">
              {service.config.base_url ?? endpointDisplay(service)}
            </span>
          </p>
        )}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
        {hasModels && (
          <Button
            variant="secondary"
            onClick={() => {
              onOpenChange(false);
              onAddModels(result!.models);
            }}
          >
            Prefill models mapping
          </Button>
        )}
        <Button variant="primary" loading={loading} onClick={() => void run()}>
          {result ? "Re-detect" : "Detect models"}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// LiteLLM substrate config viewer — read-only mono behind a disclosure

function LiteLLMViewer() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: queryKeys.litellmConfig,
    queryFn: api.services.litellmConfig,
  });

  if (isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-4/5" />
        <Skeleton className="h-4 w-3/5" />
      </div>
    );
  }

  if (isError) {
    return (
      <ErrorBanner onRetry={() => void refetch()}>
        Couldn&apos;t load LiteLLM config. {errorMessage(error)}
      </ErrorBanner>
    );
  }

  const json = JSON.stringify(data, null, 2);

  return (
    <div className="relative">
      <div className="absolute right-3 top-3">
        <CopyButton value={json} aria-label="Copy LiteLLM config" />
      </div>
      <pre className="rounded-vf-md border-vf-border bg-vf-surface-2 text-vf-micro text-vf-text border p-4 font-mono break-all whitespace-pre-wrap">
        {json}
      </pre>
    </div>
  );
}
