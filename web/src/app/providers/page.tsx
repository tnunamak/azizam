"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  CreditCard,
  DollarSign,
  ExternalLink,
  Fish,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  RotateCw,
  Search,
  ServerOff,
  ShieldCheck,
  Trash2,
  XCircle,
} from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { RelativeTime } from "@/components/product/relative-time";
import { StatusPill } from "@/components/product/status-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { adminFetch } from "@/lib/api";
import { cn } from "@/lib/cn";

type CredentialMode = "key" | "env";
type Step = "catalog" | "credentials" | "models" | "limits";

interface CatalogProvider {
  id: string;
  name: string;
  logoKey: string;
  protocol: string;
  baseUrl: string;
  docsUrl?: string;
  keyUrl?: string;
  recommendation?: string;
  billingDimensions: string[];
}

interface ProviderModel {
  id: string;
  upstreamModel: string;
  name: string;
  context?: string;
  priceInput: string;
  priceOutput: string;
  enabled: boolean;
  priceKnown: boolean;
}

interface AccessPlan {
  id: string;
  name: string;
  description?: string;
  paidAllowed?: boolean;
}

interface ProviderRecord {
  id: string;
  name: string;
  enabled: boolean | null;
  reachable: boolean | null;
  keyStatus: string;
  keySource?: string;
  keySuffix?: string;
  apiKeyEnv?: string;
  keyUpdatedAt?: number | null;
  credentialMode?: CredentialMode;
  checkedAt?: number | null;
  models: ProviderModel[];
  spendToday?: number | null;
  spendMonth?: number | null;
  capDaily?: number | null;
  capMonthly?: number | null;
  plans: string[];
  recommendation?: string;
  recommendations?: Array<{ message: string; url?: string }>;
}

interface TestResult {
  ok: boolean;
  message: string;
  models: ProviderModel[];
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function money(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `$${value.toFixed(2)}`
    : "Not measured yet";
}

function totalMoney(values: Array<number | null | undefined>): string {
  if (values.some((value) => value == null)) return "Not measured yet";
  return money(values.reduce<number>((sum, value) => sum + (value ?? 0), 0));
}

function priceNumber(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function modelLabel(value: string): string {
  const raw = value.split("/").filter(Boolean).at(-1) ?? value;
  return raw
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) =>
      /^\d+(\.\d+)?$/.test(part)
        ? part
        : part.charAt(0).toUpperCase() + part.slice(1),
    )
    .join(" ");
}

function displayModelName(
  obj: Record<string, unknown>,
  fallback: string,
): string {
  const display = text(obj.display_name, "");
  if (display) return display;
  const name = text(obj.name, "");
  if (name && !name.includes("/") && !name.includes("-")) return name;
  return modelLabel(fallback);
}

function mapModel(raw: unknown): ProviderModel {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const id = text(obj.id, text(obj.model, "model"));
  const upstreamModel = text(obj.upstream_model, id);
  const priceObj =
    obj.price && typeof obj.price === "object"
      ? (obj.price as Record<string, unknown>)
      : {};
  const inPrice = numberOrNull(obj.input_price_per_million);
  const outPrice = numberOrNull(obj.output_price_per_million);
  const nestedInput = numberOrNull(priceObj.input_usd_per_million_tokens);
  const nestedOutput = numberOrNull(priceObj.output_usd_per_million_tokens);
  const priceInput = text(
    obj.price_input,
    inPrice == null
      ? nestedInput == null
        ? ""
        : String(nestedInput)
      : String(inPrice),
  );
  const priceOutput = text(
    obj.price_output,
    outPrice == null
      ? nestedOutput == null
        ? ""
        : String(nestedOutput)
      : String(outPrice),
  );
  return {
    id,
    upstreamModel,
    name: text(
      obj.display_name,
      text(obj.name, modelLabel(upstreamModel || id)),
    ),
    context: text(obj.context, ""),
    priceInput,
    priceOutput,
    enabled: bool(obj.enabled) ?? true,
    priceKnown: Boolean(priceInput && priceOutput),
  };
}

function mapCatalog(raw: unknown): CatalogProvider[] {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const rows = Array.isArray(obj.providers)
    ? obj.providers
    : Array.isArray(raw)
      ? raw
      : [];
  return rows.map((item, index) => {
    const row =
      item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    return {
      id: text(row.id, `provider-${index}`),
      name: text(row.display_name, text(row.name, `Provider ${index + 1}`)),
      logoKey: text(row.logo_key, text(row.id, "cloud")),
      protocol: text(row.protocol, "openai-chat"),
      baseUrl: text(row.base_url, "Not supplied"),
      docsUrl: text(row.docs_url, "") || undefined,
      keyUrl: text(row.key_url, "") || undefined,
      recommendation:
        (Array.isArray(row.recommendations) &&
        row.recommendations.find((entry) => entry && typeof entry === "object")
          ? text(
              (
                row.recommendations.find(
                  (entry) => entry && typeof entry === "object",
                ) as Record<string, unknown>
              ).message,
              "",
            )
          : text(row.recommendation, "")) || undefined,
      billingDimensions: Array.isArray(row.billing_dimensions)
        ? row.billing_dimensions.map((v) => String(v))
        : [],
    };
  });
}

function mapPlans(raw: unknown): AccessPlan[] {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const rows = Array.isArray(obj.plans)
    ? obj.plans
    : Array.isArray(obj.profiles)
      ? obj.profiles
      : [];
  return rows.map((item, index) => {
    const row =
      item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    return {
      id: text(row.id, `plan-${index}`),
      name: text(row.name, text(row.label, `Plan ${index + 1}`)),
      description: text(row.description, "") || undefined,
      paidAllowed:
        bool(row.paid_allowed) ??
        bool(
          (
            row.profile as
              | { fallback?: { cloud_allowed?: boolean } }
              | undefined
          )?.fallback?.cloud_allowed,
        ) ??
        false,
    };
  });
}

function mapProviders(raw: unknown): ProviderRecord[] {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const rows = Array.isArray(obj.providers)
    ? obj.providers
    : Array.isArray(raw)
      ? raw
      : [];
  return rows.map((item, index) => {
    const row =
      item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    const keyObj =
      row.key_status && typeof row.key_status === "object"
        ? (row.key_status as Record<string, unknown>)
        : row.key && typeof row.key === "object"
          ? (row.key as Record<string, unknown>)
          : {};
    const spendObj =
      row.spend && typeof row.spend === "object"
        ? (row.spend as Record<string, unknown>)
        : {};
    const capsObj =
      row.caps && typeof row.caps === "object"
        ? (row.caps as Record<string, unknown>)
        : {};
    const recommendations = Array.isArray(row.recommendations)
      ? row.recommendations
      : [];
    const firstRecommendation = recommendations.find(
      (entry) => entry && typeof entry === "object",
    ) as Record<string, unknown> | undefined;
    return {
      id: text(row.id, text(row.provider_id, `provider-${index}`)),
      name: text(row.name, text(row.label, `Provider ${index + 1}`)),
      enabled: bool(row.enabled),
      reachable: bool(row.reachable),
      keyStatus: text(
        keyObj.state,
        text(
          row.key_status,
          bool(row.key_set) === true
            ? "set"
            : bool(row.key_set) === false
              ? "missing"
              : "unknown",
        ),
      ),
      keySource: text(keyObj.source, "") || undefined,
      keySuffix: text(keyObj.suffix, "") || undefined,
      apiKeyEnv:
        text(keyObj.api_key_env, text(row.api_key_env, "")) || undefined,
      keyUpdatedAt: numberOrNull(keyObj.updated_at),
      credentialMode:
        text(row.credential_mode, text(keyObj.source, "secret")) === "env"
          ? "env"
          : "key",
      checkedAt: numberOrNull(row.checked_at),
      models: Array.isArray(row.models) ? row.models.map(mapModel) : [],
      spendToday: numberOrNull(spendObj.daily_usd ?? row.spend_today_usd),
      spendMonth: numberOrNull(
        spendObj.monthly_usd ?? row.spend_month_usd ?? row.spend_usd,
      ),
      capDaily: numberOrNull(capsObj.daily_usd ?? row.cap_daily_usd),
      capMonthly: numberOrNull(
        capsObj.monthly_usd ?? row.cap_monthly_usd ?? row.cap_usd,
      ),
      plans: Array.isArray(row.allowed_plans)
        ? row.allowed_plans.map((v) => String(v))
        : Array.isArray(row.plans)
          ? row.plans.map((v) => String(v))
          : [],
      recommendation:
        text(firstRecommendation?.message, text(row.recommendation, "")) ||
        undefined,
    };
  });
}

async function jsonFetch(path: string, init?: RequestInit) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      body && typeof body === "object"
        ? (body as { error?: { message?: string } }).error?.message
        : null;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return body;
}

function Logo({ value }: { value: string }) {
  return (
    <span className="bg-primary text-primary-foreground inline-flex size-10 shrink-0 items-center justify-center rounded-lg text-sm font-semibold">
      {value.slice(0, 2).toUpperCase()}
    </span>
  );
}

function providerState(provider: ProviderRecord) {
  if (provider.enabled === false)
    return {
      status: "unavailable" as const,
      reason: "Configured, currently disabled.",
    };
  if (provider.keyStatus === "missing")
    return {
      status: "unavailable" as const,
      reason: "Configured, key missing.",
    };
  if (provider.reachable === false)
    return {
      status: "unavailable" as const,
      reason: "Gateway check observed it down.",
    };
  if (provider.reachable === true && provider.keyStatus === "set")
    return { status: "ready" as const, reason: "Gateway check passed." };
  return {
    status: "starting" as const,
    reason: "Configured, observation unknown.",
  };
}

function ProviderKeySummary({ provider }: { provider: ProviderRecord }) {
  if (provider.keyStatus === "missing") return <>Missing key</>;
  if (provider.keyStatus !== "set") return <>Key status unknown</>;
  const source =
    provider.keySource === "env" && provider.apiKeyEnv
      ? `Set via ${provider.apiKeyEnv}`
      : provider.keySuffix
        ? `Set · ends in ${provider.keySuffix}`
        : "Key set";
  return (
    <>
      {source}
      {provider.keyUpdatedAt != null && (
        <>
          {" "}
          · updated <RelativeTime ts={provider.keyUpdatedAt} />
        </>
      )}
    </>
  );
}

function SpendBar({
  label,
  spent,
  cap,
}: {
  label: string;
  spent?: number | null;
  cap?: number | null;
}) {
  const pct =
    spent != null && cap ? Math.min(100, Math.round((spent / cap) * 100)) : 0;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">
          {money(spent)}
          {cap ? ` of ${money(cap)}` : ""}
        </span>
      </div>
      <Progress value={pct} aria-label={`${label} spend`} />
    </div>
  );
}

function DetailCard({
  provider,
  plans,
  onAction,
}: {
  provider: ProviderRecord;
  plans: AccessPlan[];
  onAction: (action: string, provider: ProviderRecord) => void;
}) {
  const state = providerState(provider);
  const enabledModels = provider.models.filter((model) => model.enabled);
  return (
    <article className="border-border bg-card min-w-0 rounded-xl border p-5 shadow-xs">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <Logo value={provider.name} />
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">
              {provider.name}
            </h2>
            <p className="text-muted-foreground mt-1 text-sm">
              <ProviderKeySummary provider={provider} />
            </p>
          </div>
        </div>
        <StatusPill status={state.status} reason={state.reason} />
      </div>
      {provider.recommendation && (
        <div className="bg-warning/10 border-warning/30 text-foreground mt-4 rounded-lg border p-3 text-sm">
          {provider.recommendation}
        </div>
      )}
      {provider.reachable === true && provider.checkedAt != null && (
        <p className="text-muted-foreground mt-3 text-xs">
          Reachability checked <RelativeTime ts={provider.checkedAt} />.
        </p>
      )}
      <div className="mt-5 grid gap-4 lg:grid-cols-[1fr_1fr]">
        <div className="space-y-3">
          <SpendBar
            label="Today"
            spent={provider.spendToday}
            cap={provider.capDaily}
          />
          <SpendBar
            label="This month"
            spent={provider.spendMonth}
            cap={provider.capMonthly}
          />
        </div>
        <div className="rounded-lg border p-3">
          <h3 className="text-sm font-medium">Enabled models</h3>
          <div className="mt-3 flex min-w-0 flex-wrap gap-2">
            {enabledModels.length ? (
              enabledModels.map((model) => (
                <Badge
                  key={model.id}
                  variant="outline"
                  className="max-w-full break-words whitespace-normal"
                >
                  {model.name}
                </Badge>
              ))
            ) : (
              <span className="text-muted-foreground text-sm">
                No models enabled
              </span>
            )}
          </div>
          <h3 className="mt-4 text-sm font-medium">Access plans</h3>
          <div className="mt-3 flex flex-wrap gap-2">
            {plans
              .filter((plan) => provider.plans.includes(plan.id))
              .map((plan) => (
                <Badge key={plan.id} variant="secondary">
                  {plan.name}
                </Badge>
              ))}
            {!provider.plans.length && (
              <span className="text-muted-foreground text-sm">
                No paid plans allowed
              </span>
            )}
          </div>
        </div>
      </div>
      <div
        className="mt-5 max-w-full overflow-x-auto rounded-lg border"
        tabIndex={0}
        aria-label={`${provider.name} models`}
      >
        <table className="w-full min-w-0 table-fixed text-sm">
          <thead className="bg-muted/40 text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Model</th>
              <th className="px-3 py-2 text-left font-medium">Input $/M</th>
              <th className="px-3 py-2 text-left font-medium">Output $/M</th>
              <th className="px-3 py-2 text-left font-medium">State</th>
            </tr>
          </thead>
          <tbody>
            {provider.models.map((model) => (
              <tr key={model.id} className="border-t">
                <td className="px-3 py-2 font-medium break-words">
                  {model.name}
                </td>
                <td className="px-3 py-2 break-words">
                  {model.priceInput || "Unknown"}
                </td>
                <td className="px-3 py-2 break-words">
                  {model.priceOutput || "Unknown"}
                </td>
                <td className="px-3 py-2 break-words">
                  {model.enabled ? "Enabled" : "Disabled"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-5 flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onAction("test", provider)}
        >
          <RefreshCw aria-hidden="true" /> Gateway check
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onAction("rotate", provider)}
        >
          <RotateCw aria-hidden="true" /> Rotate key
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onAction("delete-key", provider)}
        >
          <KeyRound aria-hidden="true" /> Delete key
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onAction("remove", provider)}
        >
          <Trash2 aria-hidden="true" /> Remove provider
        </Button>
      </div>
    </article>
  );
}

function AddProviderDialog({
  open,
  onOpenChange,
  catalog,
  plans,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: CatalogProvider[];
  plans: AccessPlan[];
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("catalog");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<CatalogProvider | null>(null);
  const [mode, setMode] = useState<CredentialMode>("key");
  const [secret, setSecret] = useState("");
  const [envName, setEnvName] = useState("OPENROUTER_API_KEY");
  const [baseUrl, setBaseUrl] = useState("");
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [dailyCap, setDailyCap] = useState("5");
  const [monthlyCap, setMonthlyCap] = useState("50");
  const [allowedPlans, setAllowedPlans] = useState<string[]>([]);

  function resetCheck() {
    setTestResult(null);
    setModels([]);
  }

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return term
      ? catalog.filter((p) =>
          `${p.name} ${p.protocol} ${p.baseUrl}`.toLowerCase().includes(term),
        )
      : catalog;
  }, [catalog, search]);

  const testConnection = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error("Choose a provider first.");
      const raw = await adminFetch<Record<string, unknown>>(
        `/admin/api/providers/-/test`,
        {
          method: "POST",
          body: {
            preset_id: selected.id,
            provider_id: selected.id,
            name: selected.name,
            base_url: baseUrl.trim() || selected.baseUrl,
            key_mode: mode === "env" ? "env" : "secret",
            api_key: mode === "key" ? secret : undefined,
            api_key_env: mode === "env" ? envName : undefined,
            models: [],
          },
        },
      );
      const result: TestResult = {
        ok: bool(raw.ok) ?? true,
        message: text(raw.message, "Connection worked."),
        models: Array.isArray(raw.models) ? raw.models.map(mapModel) : [],
      };
      setTestResult(result);
      setModels(
        result.models.map((model) => ({ ...model, enabled: model.priceKnown })),
      );
      return result;
    },
  });

  const saveProvider = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error("Choose a provider first.");
      const missing = models.filter(
        (model) => model.enabled && (!model.priceInput || !model.priceOutput),
      );
      if (missing.length)
        throw new Error("Enter prices for each enabled paid model.");
      return adminFetch("/admin/api/providers", {
        method: "POST",
        body: {
          preset_id: selected.id,
          provider_id: selected.id,
          name: selected.name,
          base_url: baseUrl.trim() || selected.baseUrl,
          key_mode: mode === "env" ? "env" : "secret",
          api_key: mode === "key" ? secret : undefined,
          api_key_env: mode === "env" ? envName : undefined,
          models: models
            .filter((model) => model.enabled)
            .map((model) => ({
              id: model.id,
              upstream_model: model.upstreamModel,
              enabled: model.enabled,
              price: {
                input_usd_per_million_tokens: priceNumber(model.priceInput),
                output_usd_per_million_tokens: priceNumber(model.priceOutput),
              },
            })),
          caps: {
            daily_usd: Number(dailyCap),
            monthly_usd: Number(monthlyCap),
          },
          allowed_plans: allowedPlans,
          expected_generation: null,
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      onOpenChange(false);
      setStep("catalog");
    },
  });

  function toggleModel(id: string, enabled: boolean) {
    setModels((rows) =>
      rows.map((row) => (row.id === id ? { ...row, enabled } : row)),
    );
  }

  function price(
    id: string,
    field: "priceInput" | "priceOutput",
    value: string,
  ) {
    setModels((rows) =>
      rows.map((row) => (row.id === id ? { ...row, [field]: value } : row)),
    );
  }

  const canCredential =
    selected &&
    (baseUrl.trim() || selected.baseUrl) &&
    (mode === "env" ? envName.trim() : secret.trim());
  const canSave = selected && models.some((model) => model.enabled);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Add provider</DialogTitle>
          <DialogDescription>
            Connect a remote provider. Requests use it only when a model or
            alias names it.
          </DialogDescription>
        </DialogHeader>
        <div
          className="grid gap-2 sm:grid-cols-4"
          aria-label="Provider setup steps"
        >
          {(["catalog", "credentials", "models", "limits"] as Step[]).map(
            (item, index) => (
              <button
                key={item}
                type="button"
                onClick={() => setStep(item)}
                className={cn(
                  "rounded-lg border px-3 py-2 text-left text-sm capitalize",
                  step === item && "border-primary bg-primary/10 text-primary",
                )}
              >
                {index + 1}. {item}
              </button>
            ),
          )}
        </div>
        {step === "catalog" && (
          <div className="space-y-4">
            <label className="relative block">
              <Search
                className="text-muted-foreground absolute top-3 left-3 size-4"
                aria-hidden="true"
              />
              <Input
                className="pl-9"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search providers"
                aria-label="Search providers"
              />
            </label>
            <div className="grid gap-3 md:grid-cols-2">
              {filtered.map((provider) => (
                <button
                  key={provider.id}
                  type="button"
                  onClick={() => {
                    setSelected(provider);
                    setBaseUrl(
                      provider.baseUrl === "Not supplied"
                        ? ""
                        : provider.baseUrl,
                    );
                    resetCheck();
                    setStep("credentials");
                  }}
                  className={cn(
                    "hover:border-primary rounded-xl border p-4 text-left transition",
                    selected?.id === provider.id &&
                      "border-primary bg-primary/10",
                  )}
                >
                  <div className="flex gap-3">
                    <Logo value={provider.logoKey || provider.name} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 font-medium">
                        <Fish
                          className="text-primary size-4"
                          aria-hidden="true"
                        />
                        {provider.name}
                      </div>
                      <div className="text-muted-foreground mt-1 text-sm break-words">
                        {provider.protocol} · {provider.baseUrl}
                      </div>
                      <div className="mt-3 flex flex-wrap gap-1">
                        {provider.billingDimensions.map((dim) => (
                          <Badge key={dim} variant="outline">
                            {dim}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
        {step === "credentials" && selected && (
          <div className="space-y-4">
            <div className="rounded-lg border p-4">
              <div className="font-medium">{selected.name}</div>
              <label className="mt-3 block space-y-2 text-sm font-medium">
                Provider base URL
                <Input
                  aria-label="Provider base URL"
                  value={baseUrl}
                  onChange={(e) => {
                    setBaseUrl(e.target.value);
                    resetCheck();
                  }}
                  placeholder={selected.baseUrl}
                />
              </label>
              {selected.keyUrl && (
                <a
                  className="text-primary mt-2 inline-flex items-center gap-1 text-sm"
                  href={selected.keyUrl}
                >
                  Get key <ExternalLink className="size-3" />
                </a>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Button
                type="button"
                variant={mode === "key" ? "default" : "outline"}
                onClick={() => {
                  setMode("key");
                  resetCheck();
                }}
              >
                <KeyRound aria-hidden="true" /> Paste key
              </Button>
              <Button
                type="button"
                variant={mode === "env" ? "default" : "outline"}
                onClick={() => {
                  setMode("env");
                  resetCheck();
                }}
              >
                <ShieldCheck aria-hidden="true" /> Use environment variable
              </Button>
            </div>
            {mode === "key" ? (
              <Input
                aria-label="Provider API key"
                value={secret}
                onChange={(e) => {
                  setSecret(e.target.value);
                  resetCheck();
                }}
                placeholder="Provider API key"
                type="password"
              />
            ) : (
              <Input
                aria-label="Environment variable"
                value={envName}
                onChange={(e) => {
                  setEnvName(e.target.value);
                  resetCheck();
                }}
                placeholder="OPENROUTER_API_KEY"
              />
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button
                disabled={!canCredential || testConnection.isPending}
                onClick={() => testConnection.mutate()}
              >
                {testConnection.isPending ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <RefreshCw />
                )}{" "}
                Gateway check
              </Button>
              {testResult && (
                <span
                  className={cn(
                    "inline-flex items-center gap-2 text-sm",
                    testResult.ok ? "text-success" : "text-danger",
                  )}
                >
                  {testResult.ok ? (
                    <CheckCircle2 className="size-4" />
                  ) : (
                    <XCircle className="size-4" />
                  )}
                  {testResult.message}
                </span>
              )}
            </div>
          </div>
        )}
        {step === "models" && (
          <div className="space-y-3">
            {!models.length ? (
              <EmptyState
                title="No models loaded"
                description="Run Gateway check to read models from the provider."
                icon={Cloud}
              />
            ) : (
              models.map((model) => (
                <div
                  key={model.id}
                  className="grid gap-3 rounded-lg border p-3 sm:grid-cols-[minmax(0,1fr)_120px_120px_80px]"
                >
                  <label className="flex items-start gap-3">
                    <Checkbox
                      checked={model.enabled}
                      onCheckedChange={(checked) =>
                        toggleModel(model.id, checked === true)
                      }
                      aria-label={`Enable ${model.name}`}
                    />
                    <span>
                      <span className="block font-medium">{model.name}</span>
                      <span className="text-muted-foreground text-sm">
                        {model.context || "Context not reported"}
                      </span>
                    </span>
                  </label>
                  <Input
                    aria-label={`${model.name} input price`}
                    value={model.priceInput}
                    onChange={(e) =>
                      price(model.id, "priceInput", e.target.value)
                    }
                    placeholder="Input $/M"
                  />
                  <Input
                    aria-label={`${model.name} output price`}
                    value={model.priceOutput}
                    onChange={(e) =>
                      price(model.id, "priceOutput", e.target.value)
                    }
                    placeholder="Output $/M"
                  />
                  <Badge
                    variant={
                      model.priceInput && model.priceOutput
                        ? "secondary"
                        : "outline"
                    }
                  >
                    {model.priceInput && model.priceOutput
                      ? "Priced"
                      : "Needs price"}
                  </Badge>
                </div>
              ))
            )}
          </div>
        )}
        {step === "limits" && (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-2 text-sm font-medium">
                Daily cap{" "}
                <Input
                  aria-label="Daily cap"
                  value={dailyCap}
                  onChange={(e) => setDailyCap(e.target.value)}
                />
              </label>
              <label className="space-y-2 text-sm font-medium">
                Monthly cap{" "}
                <Input
                  aria-label="Monthly cap"
                  value={monthlyCap}
                  onChange={(e) => setMonthlyCap(e.target.value)}
                />
              </label>
            </div>
            <div className="space-y-3">
              <h3 className="text-sm font-medium">Access plans</h3>
              {allowedPlans.length === 0 && (
                <p className="text-muted-foreground rounded-lg border p-3 text-sm">
                  No plans selected. Paid requests will be denied until an owner
                  allows a plan.
                </p>
              )}
              {plans.map((plan) => (
                <label
                  key={plan.id}
                  className="flex items-start justify-between gap-3 rounded-lg border p-3"
                >
                  <span>
                    <span className="block font-medium">{plan.name}</span>
                    <span className="text-muted-foreground text-sm">
                      {plan.description ||
                        (plan.paidAllowed
                          ? "Paid providers allowed"
                          : "Local only by default")}
                    </span>
                  </span>
                  <Switch
                    checked={allowedPlans.includes(plan.id)}
                    onCheckedChange={(checked) =>
                      setAllowedPlans((rows) =>
                        checked
                          ? [...new Set([...rows, plan.id])]
                          : rows.filter((id) => id !== plan.id),
                      )
                    }
                    aria-label={`Allow ${plan.name}`}
                  />
                </label>
              ))}
            </div>
            {saveProvider.error && (
              <div role="alert" className="text-danger text-sm">
                {saveProvider.error instanceof Error
                  ? saveProvider.error.message
                  : "Save failed."}
              </div>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          {step !== "catalog" && (
            <Button
              variant="outline"
              onClick={() =>
                setStep(
                  step === "credentials"
                    ? "catalog"
                    : step === "models"
                      ? "credentials"
                      : "models",
                )
              }
            >
              Back
            </Button>
          )}
          {step !== "limits" ? (
            <Button
              onClick={() =>
                setStep(
                  step === "catalog"
                    ? "credentials"
                    : step === "credentials"
                      ? "models"
                      : "limits",
                )
              }
              disabled={
                (step === "catalog" && !selected) ||
                (step === "credentials" && testResult?.ok !== true)
              }
            >
              Next
            </Button>
          ) : (
            <Button
              onClick={() => saveProvider.mutate()}
              disabled={!canSave || saveProvider.isPending}
            >
              {saveProvider.isPending ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Plus />
              )}{" "}
              Save provider
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function ProvidersPage() {
  const [addOpen, setAddOpen] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<{
    action: "delete-key" | "remove";
    provider: ProviderRecord;
  } | null>(null);
  const queryClient = useQueryClient();
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => jsonFetch("/admin/api/providers").then(mapProviders),
    retry: false,
  });
  const catalog = useQuery({
    queryKey: ["providers", "catalog"],
    queryFn: () => jsonFetch("/admin/api/providers/-/catalog").then(mapCatalog),
    retry: false,
  });
  const plans = useQuery({
    queryKey: ["providers", "plans"],
    queryFn: () => jsonFetch("/admin/api/profiles").then(mapPlans),
    retry: false,
  });
  const action = useMutation({
    mutationFn: async ({
      action: name,
      provider,
    }: {
      action: string;
      provider: ProviderRecord;
    }) => {
      const encoded = encodeURIComponent(provider.id);
      if (name === "test") {
        return adminFetch(`/admin/api/providers/${encoded}/test`, {
          method: "POST",
          body: {},
        });
      }
      if (name === "delete-key") {
        return adminFetch(`/admin/api/providers/${encoded}/key`, {
          method: "DELETE",
          body: {},
        });
      }
      if (name === "remove") {
        return adminFetch(`/admin/api/providers/${encoded}`, {
          method: "DELETE",
          body: { expected_generation: null },
        });
      }
      if (name === "rotate") {
        const replacement = window.prompt(
          `Paste a replacement key for ${provider.name}. It will not be shown again.`,
        );
        if (!replacement) throw new Error("Rotation cancelled.");
        return adminFetch(`/admin/api/providers/${encoded}/key`, {
          method: "PUT",
          body: { api_key: replacement },
        });
      }
      throw new Error("Unknown provider action.");
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });

  const loading = providers.isPending || catalog.isPending || plans.isPending;
  const missingApi =
    providers.error instanceof Error &&
    /404|not found/i.test(providers.error.message);
  const records = providers.data ?? [];
  const catalogRows = catalog.data ?? [];
  const planRows = plans.data ?? [];

  return (
    <>
      <PageHeader
        title="Providers"
        answer="Remote providers are explicit paid routes."
        description="Requests go to a remote provider only when a model or alias names it. We never switch to a paid provider on our own."
        actions={
          <Button onClick={() => setAddOpen(true)}>
            <Plus aria-hidden="true" /> Add provider
          </Button>
        }
      />
      <div className="pb-20 sm:pb-0">
        {loading ? (
          <div
            role="status"
            aria-label="Loading providers"
            aria-busy="true"
            className="space-y-4"
          >
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : missingApi ? (
          <EmptyState
            title="Provider controls are not installed"
            description="This gateway does not expose the provider admin API yet. Local engines and explicit model aliases keep working."
            icon={ServerOff}
          />
        ) : providers.isError ? (
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-lg border p-4 text-sm"
          >
            <p>
              {providers.error instanceof Error
                ? providers.error.message
                : "Provider status failed."}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => providers.refetch()}
            >
              <RefreshCw aria-hidden="true" /> Retry providers
            </Button>
          </div>
        ) : records.length === 0 ? (
          <EmptyState
            title="No providers yet"
            description="Connect a provider to use cloud models alongside your local ones. Requests only go to a provider when a model names it."
            icon={Fish}
            action={
              <Button onClick={() => setAddOpen(true)}>
                <Plus aria-hidden="true" /> Add provider
              </Button>
            }
          />
        ) : (
          <div className="space-y-5">
            <section className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-xl border p-4">
                <DollarSign className="text-primary size-5" />
                <div className="mt-3 text-2xl font-semibold">
                  {totalMoney(records.map((row) => row.spendMonth))}
                </div>
                <p className="text-muted-foreground text-sm">
                  Remote spend this month
                </p>
              </div>
              <div className="rounded-xl border p-4">
                <CreditCard className="text-primary size-5" />
                <div className="mt-3 text-2xl font-semibold">
                  {
                    records.filter((row) =>
                      row.models.some((model) => model.enabled),
                    ).length
                  }
                </div>
                <p className="text-muted-foreground text-sm">
                  Providers with enabled models
                </p>
              </div>
              <div className="rounded-xl border p-4">
                <ShieldCheck className="text-primary size-5" />
                <div className="mt-3 text-2xl font-semibold">
                  {new Set(records.flatMap((row) => row.plans)).size}
                </div>
                <p className="text-muted-foreground text-sm">
                  Plans allowed by configured providers
                </p>
              </div>
            </section>
            <div className="grid gap-4 xl:grid-cols-2">
              {records.map((provider) => (
                <DetailCard
                  key={provider.id}
                  provider={provider}
                  plans={planRows}
                  onAction={(name, providerRecord) =>
                    name === "delete-key" || name === "remove"
                      ? setConfirmTarget({
                          action: name,
                          provider: providerRecord,
                        })
                      : action.mutate({
                          action: name,
                          provider: providerRecord,
                        })
                  }
                />
              ))}
            </div>
            {action.error && (
              <div role="alert" className="text-danger text-sm">
                <AlertCircle className="mr-1 inline size-4" />
                {action.error instanceof Error
                  ? action.error.message
                  : "Provider action failed."}
              </div>
            )}
          </div>
        )}
      </div>
      <AddProviderDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        catalog={catalogRows}
        plans={planRows}
      />
      <ConfirmDialog
        open={confirmTarget !== null}
        onOpenChange={(open) => !open && setConfirmTarget(null)}
        title={
          confirmTarget?.action === "remove"
            ? "Remove provider"
            : "Delete provider key"
        }
        description={
          confirmTarget
            ? `This changes ${confirmTarget.provider.name}. The full key is never shown again.`
            : undefined
        }
        confirmLabel={
          confirmTarget?.action === "remove" ? "Remove provider" : "Delete key"
        }
        typedName={confirmTarget?.provider.name}
        onConfirm={async () => {
          if (!confirmTarget) return;
          await action.mutateAsync({
            action: confirmTarget.action,
            provider: confirmTarget.provider,
          });
          setConfirmTarget(null);
        }}
      />
    </>
  );
}
