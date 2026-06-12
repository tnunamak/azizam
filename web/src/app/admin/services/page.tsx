"use client";

/** Admin → Services — backend service registry.
 *
 * Spec: docs/design/WEB_UI_STEERING.md "Services" section.
 * Contract: docs/design/web-api-contract.md "Surface 2 — Services".
 *
 * Presentation law: RelativeTime for every timestamp, EntityRef for every id,
 * no window.confirm, destructive actions in OverflowMenu + ConfirmDialog,
 * loading skeleton + designed empty + error states.
 *
 * PROVIDER_PRESETS are hardcoded here (matching gateway/service_registry.py)
 * because they are not exposed over a JSON endpoint. Any change to the Python
 * source must be reflected here. */

import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { adminFetch, ApiError } from "@/lib/api";
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
// Types — local to this surface (not yet in api.ts)

interface ServiceConfig {
  host?: string;
  port?: number;
  base_url?: string;
}

interface Service {
  id: string;
  name: string;
  kind: string;
  source: "provisioned" | "ui";
  enabled: boolean;
  capabilities: string[];
  config: ServiceConfig;
}

interface ServiceRegistry {
  schema_version: number;
  services: Record<string, Service>;
  protocol_frontends?: Record<string, unknown>;
  workflows?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
}

interface ServiceRegistryResponse {
  service_registry: ServiceRegistry;
}

interface TestProbe {
  ok: boolean;
  status: number | null;
  error: string;
  url: string;
}

interface TestResponse {
  probe: TestProbe;
}

interface DetectResponse {
  detected: { ok: boolean; models: string[]; error: string };
}

interface LiteLLMConfigResponse {
  litellm_config: {
    model_list: Array<{ model_name: string; litellm_params: Record<string, unknown> }>;
    general_settings?: Record<string, unknown>;
  };
}

// ---------------------------------------------------------------------------
// Provider presets — matches PROVIDER_PRESETS in gateway/service_registry.py.
// Not exposed over JSON; hardcoded here and must stay in sync with the Python.

interface ProviderPreset {
  id: string;
  label: string;
  base_path: string;
  default_port: number | null;
  protocol: string | null;
  capabilities: string[];
  native_passthrough: boolean;
}

const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: "openai-compatible",
    label: "OpenAI-compatible (custom)",
    base_path: "/v1",
    default_port: null,
    protocol: "openai-compatible",
    capabilities: ["chat", "completions"],
    native_passthrough: true,
  },
  {
    id: "vllm",
    label: "vLLM",
    base_path: "/v1",
    default_port: 8000,
    protocol: "openai-compatible",
    capabilities: ["chat", "completions"],
    native_passthrough: true,
  },
  {
    id: "ollama",
    label: "Ollama",
    base_path: "/v1",
    default_port: 11434,
    protocol: "openai-compatible",
    capabilities: ["chat", "completions"],
    native_passthrough: true,
  },
  {
    id: "tabbyapi",
    label: "TabbyAPI",
    base_path: "/v1",
    default_port: 5000,
    protocol: "openai-compatible",
    capabilities: ["chat", "completions", "vision"],
    native_passthrough: true,
  },
  {
    id: "lmstudio",
    label: "LM Studio",
    base_path: "/v1",
    default_port: 1234,
    protocol: "openai-compatible",
    capabilities: ["chat", "completions"],
    native_passthrough: true,
  },
  {
    id: "comfyui",
    label: "ComfyUI",
    base_path: "",
    default_port: 8188,
    protocol: null,
    capabilities: ["image"],
    native_passthrough: false,
  },
];

// ---------------------------------------------------------------------------
// Query keys for this surface

const serviceQueryKeys = {
  serviceRegistry: ["serviceRegistry"] as const,
  litellmConfig: ["litellmConfig"] as const,
};

// ---------------------------------------------------------------------------
// API helpers

async function fetchServiceRegistry(): Promise<ServiceRegistry> {
  const r = await adminFetch<ServiceRegistryResponse>(
    "/admin/api/operator/service-registry",
  );
  return r.service_registry;
}

async function addService(
  input: Record<string, unknown>,
): Promise<ServiceRegistry> {
  const r = await adminFetch<ServiceRegistryResponse>(
    "/admin/api/operator/services",
    { method: "POST", body: input },
  );
  return r.service_registry;
}

async function editService(
  serviceId: string,
  input: Record<string, unknown>,
): Promise<ServiceRegistry> {
  const r = await adminFetch<ServiceRegistryResponse>(
    `/admin/api/operator/services/${serviceId}`,
    { method: "PATCH", body: input },
  );
  return r.service_registry;
}

async function removeService(serviceId: string): Promise<ServiceRegistry> {
  const r = await adminFetch<ServiceRegistryResponse>(
    `/admin/api/operator/services/${serviceId}`,
    { method: "DELETE" },
  );
  return r.service_registry;
}

async function toggleEnabled(
  serviceId: string,
  enabled: boolean,
): Promise<ServiceRegistry> {
  const r = await adminFetch<ServiceRegistryResponse>(
    `/admin/api/operator/services/${serviceId}/enabled`,
    { method: "POST", body: { enabled } },
  );
  return r.service_registry;
}

async function testConnection(
  input: Record<string, unknown>,
): Promise<TestProbe> {
  const r = await adminFetch<TestResponse>(
    "/admin/api/operator/services/test",
    { method: "POST", body: input },
  );
  return r.probe;
}

async function detectModels(
  input: Record<string, unknown>,
): Promise<{ ok: boolean; models: string[]; error: string }> {
  const r = await adminFetch<DetectResponse>(
    "/admin/api/operator/services/detect-models",
    { method: "POST", body: input },
  );
  return r.detected;
}

async function fetchLiteLLMConfig(): Promise<LiteLLMConfigResponse["litellm_config"]> {
  const r = await adminFetch<LiteLLMConfigResponse>(
    "/admin/api/operator/litellm-config",
  );
  return r.litellm_config;
}

// ---------------------------------------------------------------------------
// Capability id → human label map (matches _CAPABILITY_CHOICES in service_registry.py).
// Fall back to sentence-case of the raw id for unknowns so new ids are readable.

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
  // Sentence-case fallback: replace dots/underscores with spaces, capitalise first letter.
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
    queryKey: serviceQueryKeys.serviceRegistry,
    queryFn: fetchServiceRegistry,
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
  const [litellmOpen, setLitellmOpen] = useState(false);
  /** Banner shown after any write to remind about gateway restart. */
  const [restartBanner, setRestartBanner] = useState(false);

  function showRestartBanner() {
    setRestartBanner(true);
  }

  // Toggle enabled — optimistic update
  const toggleMutation = useMutation({
    mutationFn: ({ serviceId, enabled }: { serviceId: string; enabled: boolean }) =>
      toggleEnabled(serviceId, enabled),
    onMutate: async ({ serviceId, enabled }) => {
      await queryClient.cancelQueries({ queryKey: serviceQueryKeys.serviceRegistry });
      const previous = queryClient.getQueryData<ServiceRegistry>(
        serviceQueryKeys.serviceRegistry,
      );
      queryClient.setQueryData<ServiceRegistry>(
        serviceQueryKeys.serviceRegistry,
        (reg) => {
          if (!reg) return reg;
          return {
            ...reg,
            services: {
              ...reg.services,
              [serviceId]: { ...reg.services[serviceId], enabled },
            },
          };
        },
      );
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(serviceQueryKeys.serviceRegistry, context.previous);
      }
      toast.error(`Couldn't update service`, { description: errorMessage(err) });
    },
    onSuccess: (newRegistry) => {
      queryClient.setQueryData(serviceQueryKeys.serviceRegistry, newRegistry);
      showRestartBanner();
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: serviceQueryKeys.serviceRegistry }),
  });

  // Remove mutation
  const removeMutation = useMutation({
    mutationFn: (serviceId: string) => removeService(serviceId),
    onSuccess: (newRegistry) => {
      queryClient.setQueryData(serviceQueryKeys.serviceRegistry, newRegistry);
      toast.success("Service removed");
      showRestartBanner();
      setRemoveTarget(null);
    },
    onError: (err) => {
      toast.error("Couldn't remove service", { description: errorMessage(err) });
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: serviceQueryKeys.serviceRegistry }),
  });

  const columns: DataTableColumn<Service>[] = [
    {
      id: "service",
      header: "Service",
      cell: (svc) => (
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <EntityRef name={svc.name} id={svc.id} />
          <SourceBadge source={svc.source} />
        </span>
      ),
    },
    {
      id: "endpoint",
      header: "Endpoint",
      cell: (svc) => (
        <span className="text-vf-micro text-vf-subtle font-mono">
          {endpointDisplay(svc)}
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
        <Switch
          checked={svc.enabled}
          disabled={toggleMutation.isPending}
          aria-label={`Toggle ${svc.name}`}
          onCheckedChange={(checked) =>
            toggleMutation.mutate({ serviceId: svc.id, enabled: checked })
          }
        />
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
            empty={
              <EmptyState
                action={
                  <Button onClick={() => setAddOpen(true)}>Add service</Button>
                }
              >
                No services configured. Add one to route traffic.
              </EmptyState>
            }
            rowMenu={(svc) => (
              <OverflowMenu label={`Actions for ${svc.name}`}>
                <OverflowMenuItem onSelect={() => setEditTarget(svc)}>
                  Edit…
                </OverflowMenuItem>
                <OverflowMenuItem onSelect={() => setTestTarget(svc)}>
                  Test connection…
                </OverflowMenuItem>
                <OverflowMenuItem onSelect={() => setDetectTarget(svc)}>
                  Detect models…
                </OverflowMenuItem>
                {svc.source === "ui" && (
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
            )}
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
          queryClient.setQueryData(serviceQueryKeys.serviceRegistry, newRegistry);
          void queryClient.invalidateQueries({ queryKey: serviceQueryKeys.serviceRegistry });
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
          queryClient.setQueryData(serviceQueryKeys.serviceRegistry, newRegistry);
          void queryClient.invalidateQueries({ queryKey: serviceQueryKeys.serviceRegistry });
          setEditTarget(null);
          showRestartBanner();
          toast.success("Service updated");
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
      />

      {/* Remove confirm — typed-name for safety; UI-source only */}
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null);
        }}
        title="Remove service?"
        description={
          removeTarget
            ? `${removeTarget.name} will be removed from the registry. Routes that depend on it will stop working after the next gateway restart.`
            : undefined
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
// Source badge with lock icon for provisioned (non-editable) rows

function SourceBadge({ source }: { source: "provisioned" | "ui" }) {
  return source === "provisioned" ? (
    <Badge sm tone="neutral">
      🔒 config
    </Badge>
  ) : (
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
// Service form — shared by Add and Edit

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
  /** When true, show advanced fields expanded. */
  showAdvanced?: boolean;
  onToggleAdvanced?: () => void;
  /** Probe result from "Test connection" run inline in the Add dialog. */
  probeResult?: TestProbe | null;
  onTest?: () => void;
  testing?: boolean;
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
}: ServiceFormProps) {
  const set = (key: keyof ServiceFormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
  ) => onChange({ ...value, [key]: e.target.value });

  return (
    <div className="flex flex-col gap-4">
      <Field label="Provider" help="Pre-fills connection defaults for common upstream types.">
        <Select value={value.preset} onChange={set("preset")} disabled={disabled}>
          {PROVIDER_PRESETS.map((p) => (
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

function emptyFormState(): ServiceFormState {
  return { name: "", preset: "openai-compatible", base_url: "", api_key_env: "" };
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
// Add service dialog

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
  const [form, setForm] = useState<ServiceFormState>(emptyFormState());
  const [formError, setFormError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);

  const save = useMutation({
    mutationFn: () => addService(formToApiBody(form)),
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
      const result = await testConnection({ base_url: form.base_url.trim() });
      setProbe(result);
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  return (
    <DialogContent>
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Add service</DialogTitle>
          <DialogDescription>
            Register an upstream backend. Changes take effect after a gateway restart.
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
      </form>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Edit service dialog

function EditServiceDialog({
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
        <EditServiceDialogContent
          service={service}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

function EditServiceDialogContent({
  service,
  onOpenChange,
  onSaved,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const isReadOnly = service.source === "provisioned";

  const [form, setForm] = useState<ServiceFormState>(() => ({
    name: service.name,
    preset: "openai-compatible",
    base_url: service.config.base_url ?? "",
    api_key_env: "",
  }));
  const [formError, setFormError] = useState<string | null>(null);
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);

  const save = useMutation({
    mutationFn: () => editService(service.id, formToApiBody(form)),
    onSuccess: onSaved,
    onError: (err) => setFormError(errorMessage(err)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (isReadOnly) return;
    setFormError(null);
    if (!form.name.trim()) {
      setFormError("Service name is required.");
      return;
    }
    save.mutate();
  }

  async function handleTest() {
    setTesting(true);
    setProbe(null);
    try {
      const result = await testConnection({ base_url: form.base_url.trim() });
      setProbe(result);
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  return (
    <DialogContent>
      <form onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>
            {isReadOnly ? `${service.name} (read-only)` : `Edit ${service.name}`}
          </DialogTitle>
          <DialogDescription>
            {isReadOnly
              ? "This service was loaded from the gateway config file and cannot be edited here."
              : "Changes take effect after a gateway restart."}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {isReadOnly ? (
            <ReadOnlyServiceView service={service} />
          ) : (
            <ServiceForm
              value={form}
              onChange={setForm}
              disabled={save.isPending}
              error={formError}
              probeResult={probe}
              onTest={() => void handleTest()}
              testing={testing}
            />
          )}
        </DialogBody>
        <DialogFooter>
          {isReadOnly ? (
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          ) : (
            <>
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
            </>
          )}
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

function ReadOnlyServiceView({ service }: { service: Service }) {
  return (
    <dl className="flex flex-col gap-3">
      <div>
        <dt className="text-vf-micro text-vf-subtle uppercase tracking-wider">
          ID
        </dt>
        <dd className="text-vf-body text-vf-text font-mono">{service.id}</dd>
      </div>
      <div>
        <dt className="text-vf-micro text-vf-subtle uppercase tracking-wider">
          Kind
        </dt>
        <dd className="text-vf-body text-vf-text">{service.kind}</dd>
      </div>
      <div>
        <dt className="text-vf-micro text-vf-subtle uppercase tracking-wider">
          Endpoint
        </dt>
        <dd className="text-vf-micro text-vf-text font-mono">
          {service.config.base_url ??
            `${service.config.host ?? ""}:${service.config.port ?? ""}`}
        </dd>
      </div>
      <div>
        <dt className="text-vf-micro text-vf-subtle uppercase tracking-wider">
          Capabilities
        </dt>
        <dd className="mt-1 flex flex-wrap gap-1">
          {service.capabilities.map((cap) => (
            <Badge key={cap} sm>
              {cap}
            </Badge>
          ))}
        </dd>
      </div>
    </dl>
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
      const result = await testConnection(endpoint);
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
// Detect models dialog

function DetectModelsDialog({
  service,
  onOpenChange,
}: {
  service: Service | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={service !== null} onOpenChange={onOpenChange}>
      {service && (
        <DetectModelsDialogContent service={service} onOpenChange={onOpenChange} />
      )}
    </Dialog>
  );
}

function DetectModelsDialogContent({
  service,
  onOpenChange,
}: {
  service: Service;
  onOpenChange: (open: boolean) => void;
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
      const detected = await detectModels(endpoint);
      setResult(detected);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

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
    queryKey: serviceQueryKeys.litellmConfig,
    queryFn: fetchLiteLLMConfig,
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
