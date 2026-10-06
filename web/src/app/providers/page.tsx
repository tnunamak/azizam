"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  ExternalLink,
  Fish,
  KeyRound,
  Loader2,
  MinusCircle,
  MoreHorizontal,
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
  spendStatus?: string;
  pendingReconciliationCount: number | null;
  needsReconciliation: boolean;
  capDaily?: number | null;
  capMonthly?: number | null;
  plans: string[];
  recommendation?: string;
  recommendations?: Array<{ message: string; url?: string }>;
}

interface GlobalProviderState {
  spendToday?: number | null;
  spendMonth?: number | null;
  spendStatus?: string;
  pendingReconciliationCount: number | null;
  needsReconciliation: boolean;
  capDaily?: number | null;
  capMonthly?: number | null;
}

interface TestResult {
  ok: boolean;
  message: string;
  models: ProviderModel[];
}

interface ReconciliationRow {
  id: number;
  provider: string;
  profileId: string;
  status: string;
  estimatedUsd: number | null;
  actualUsd: number | null;
  estimatedUsdText: string;
  actualUsdText: string;
  costSource: string;
  createdAt: number | null;
  settledAt: number | null;
  requestId: string;
  generationId: string;
}

interface ReconciliationPayload {
  reconciliations: ReconciliationRow[];
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

function nonnegativeNumberOrNull(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) && value >= 0 ? value : null;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value.trim());
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  }
  return null;
}

function money(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "Not measured yet";
  }
  if (value > 0 && value < 0.01) {
    return "<$0.01";
  }
  return `$${value.toFixed(2)}`;
}

function countOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.trunc(value)
    : 0;
}

function preciseUsd(
  value: string,
  fallback: number | null | undefined,
): string {
  const trimmed = value.trim();
  if (trimmed) return `$${trimmed}`;
  return money(fallback);
}

function pendingCountFromSpend(
  spendObj: Record<string, unknown>,
): number | null {
  return "pending_reconciliation_count" in spendObj
    ? countOrZero(spendObj.pending_reconciliation_count)
    : null;
}

function needsReconciliationFromSpend(
  spendObj: Record<string, unknown>,
): boolean {
  return (
    text(spendObj.status, "") === "needs_reconciliation" ||
    countOrZero(spendObj.pending_reconciliation_count) > 0
  );
}

function reconciliationLabel(count: number | null | undefined): string {
  if (count == null || count === 0) return "Charges need reconciliation";
  return `${count} charge${count === 1 ? " needs" : "s need"} reconciliation`;
}

function decimalInputValid(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(trimmed)) {
    return false;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0;
}

function formatIdentity(value: string): string {
  return value || "Not recorded";
}

function priceNumber(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
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
  const nestedInput = nonnegativeNumberOrNull(
    priceObj.input_usd_per_million_tokens,
  );
  const nestedOutput = nonnegativeNumberOrNull(
    priceObj.output_usd_per_million_tokens,
  );
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
    name: text(obj.name, upstreamModel || id),
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
      baseUrl: text(row.base_url, ""),
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
      id: text(
        row.policy_id,
        text(
          (row.profile as { id?: unknown } | undefined)?.id,
          text(row.id, `plan-${index}`),
        ),
      ),
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
    const id = text(row.id, text(row.provider_id, `provider-${index}`));
    const declaredName = text(row.name, text(row.label, ""));
    const name =
      declaredName && declaredName !== id ? declaredName : "Remote provider";
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
    const spendStatus = text(spendObj.status, "known");
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
      id,
      name,
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
      spendToday:
        spendStatus === "unknown"
          ? null
          : numberOrNull(spendObj.daily_usd ?? row.spend_today_usd),
      spendMonth:
        spendStatus === "unknown"
          ? null
          : numberOrNull(
              spendObj.monthly_usd ?? row.spend_month_usd ?? row.spend_usd,
            ),
      spendStatus,
      pendingReconciliationCount: pendingCountFromSpend(spendObj),
      needsReconciliation: needsReconciliationFromSpend(spendObj),
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

function mapGlobal(raw: unknown): GlobalProviderState {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const globalObj =
    obj.global && typeof obj.global === "object"
      ? (obj.global as Record<string, unknown>)
      : {};
  const spendObj =
    globalObj.spend && typeof globalObj.spend === "object"
      ? (globalObj.spend as Record<string, unknown>)
      : {};
  const capsObj =
    globalObj.caps && typeof globalObj.caps === "object"
      ? (globalObj.caps as Record<string, unknown>)
      : {};
  const spendStatus = text(spendObj.status, "known");
  return {
    spendToday:
      spendStatus === "unknown" ? null : numberOrNull(spendObj.daily_usd),
    spendMonth:
      spendStatus === "unknown" ? null : numberOrNull(spendObj.monthly_usd),
    spendStatus,
    pendingReconciliationCount: pendingCountFromSpend(spendObj),
    needsReconciliation: needsReconciliationFromSpend(spendObj),
    capDaily: numberOrNull(capsObj.daily_usd),
    capMonthly: numberOrNull(capsObj.monthly_usd),
  };
}

function mapReconciliations(raw: unknown): ReconciliationPayload {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const rows = Array.isArray(obj.reconciliations) ? obj.reconciliations : [];
  return {
    reconciliations: rows.map((item) => {
      const row =
        item && typeof item === "object"
          ? (item as Record<string, unknown>)
          : {};
      return {
        id: Number(row.id),
        provider: text(row.provider, ""),
        profileId: text(row.profile_id, ""),
        status: text(row.status, "unknown"),
        estimatedUsd: nonnegativeNumberOrNull(row.estimated_usd),
        actualUsd: nonnegativeNumberOrNull(row.actual_usd),
        estimatedUsdText: text(row.estimated_usd, ""),
        actualUsdText: text(row.actual_usd, ""),
        costSource: text(row.cost_source, "unknown"),
        createdAt: numberOrNull(row.created_at),
        settledAt: numberOrNull(row.settled_at),
        requestId: text(row.request_id, ""),
        generationId: text(row.generation_id, ""),
      };
    }),
  };
}

function modelPayload(models: ProviderModel[]) {
  return models.map((model) => ({
    id: model.id,
    name: model.name,
    upstream_model: model.upstreamModel,
    enabled: model.enabled,
    price: {
      input_usd_per_million_tokens: priceNumber(model.priceInput),
      output_usd_per_million_tokens: priceNumber(model.priceOutput),
    },
  }));
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
      reason: "Provider connection check observed it down.",
    };
  if (provider.reachable === true && provider.keyStatus === "set")
    return {
      status: "ready" as const,
      reason: "Provider connection check passed.",
    };
  return {
    status: "unknown" as const,
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
        ? `Key ends in ${provider.keySuffix}`
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
    spent != null && cap != null && cap > 0
      ? Math.min(100, Math.round((spent / cap) * 100))
      : 0;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">
          {money(spent)}
          {cap != null ? ` of ${money(cap)}` : ""}
        </span>
      </div>
      <Progress value={pct} aria-label={`${label} spend`} />
    </div>
  );
}

function capSummary(daily?: number | null, monthly?: number | null): string {
  const parts = [
    daily == null ? null : `${money(daily)} per day`,
    monthly == null ? null : `${money(monthly)} per month`,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No cap set";
}

function DetailCard({
  provider,
  plans,
  busy,
  onAction,
  onSaveModels,
  onSaveCaps,
  onSavePlans,
}: {
  provider: ProviderRecord;
  plans: AccessPlan[];
  busy?: boolean;
  onAction: (action: string, provider: ProviderRecord) => void;
  onSaveModels: (provider: ProviderRecord, models: ProviderModel[]) => void;
  onSaveCaps: (
    provider: ProviderRecord,
    daily: string,
    monthly: string,
  ) => void;
  onSavePlans: (provider: ProviderRecord, allowedPlans: string[]) => void;
}) {
  const state = providerState(provider);
  const enabledModels = provider.models.filter((model) => model.enabled);
  const [modelDraft, setModelDraft] = useState<ProviderModel[]>(
    provider.models,
  );
  const [capsDraft, setCapsDraft] = useState({
    daily: provider.capDaily == null ? "" : String(provider.capDaily),
    monthly: provider.capMonthly == null ? "" : String(provider.capMonthly),
  });
  const [planDraft, setPlanDraft] = useState<string[]>(provider.plans);
  const [editing, setEditing] = useState<"models" | "caps" | "plans" | null>(
    null,
  );
  function updateModel(id: string, patch: Partial<ProviderModel>) {
    setModelDraft((rows) =>
      rows.map((row) => (row.id === id ? { ...row, ...patch } : row)),
    );
  }
  return (
    <article className="border-border bg-card min-w-0 overflow-hidden rounded-xl border shadow-xs">
      <details className="group">
        <summary className="list-none marker:hidden">
          <div className="grid cursor-pointer gap-3 p-4 sm:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-center sm:px-5">
            <div className="flex min-w-0 gap-3 sm:items-center">
              <Logo value={provider.name} />
              <div className="min-w-0">
                <h2 className="truncate text-base font-semibold">
                  {provider.name}
                </h2>
                <span className="border-border bg-muted text-muted-foreground mt-1 inline-flex min-h-6 items-center rounded-full border px-2 text-xs">
                  <ProviderKeySummary provider={provider} />
                </span>
              </div>
            </div>
            <StatusPill status={state.status} reason={state.reason} summary />
            <div className="min-w-0 space-y-2">
              {provider.spendMonth != null ? (
                <SpendBar
                  label="This month"
                  spent={provider.spendMonth}
                  cap={provider.capMonthly}
                />
              ) : provider.capMonthly != null ? (
                <p className="text-muted-foreground text-xs">
                  Monthly cap: {money(provider.capMonthly)}
                </p>
              ) : null}
              {provider.capDaily != null && (
                <p className="text-muted-foreground text-xs">
                  Daily cap: {money(provider.capDaily)}
                </p>
              )}
            </div>
            <span className="border-input bg-background hover:bg-accent hover:text-accent-foreground inline-flex h-8 w-full cursor-pointer items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors">
              Manage
            </span>
          </div>
        </summary>
        <div className="border-t px-4 pt-4 pb-4 sm:px-5">
          <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
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
              {provider.needsReconciliation && (
                <p className="text-muted-foreground text-xs">
                  Totals include estimated charges that are not final bills.
                </p>
              )}
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
                      {model.id}
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
                    No access plans can use this provider
                  </span>
                )}
              </div>
            </div>
          </div>
          <details className="mt-3 text-sm">
            <summary className="text-muted-foreground min-h-11 cursor-pointer content-center">
              Provider details
            </summary>
            <p className="text-muted-foreground pb-3 text-xs break-all">
              Provider ID: {provider.id}
            </p>
          </details>
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
          {provider.needsReconciliation && (
            <div className="border-warning/30 bg-warning/10 mt-4 rounded-lg border p-3 text-sm">
              <div className="font-medium">
                {reconciliationLabel(provider.pendingReconciliationCount)}
              </div>
              <p className="text-muted-foreground mt-1">
                Spend totals include estimates until an owner records the
                verified provider charge. Reconcile before removing this
                provider.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={() => onAction("reconcile", provider)}
              >
                Reconcile charges
              </Button>
            </div>
          )}
          <div
            className="mt-5 max-w-full overflow-x-auto rounded-lg border"
            tabIndex={0}
            aria-label={`${provider.name} models`}
          >
            <table className="w-full min-w-0 table-fixed text-sm">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">Model</th>
                  <th className="px-3 py-2 text-left font-medium">
                    Input: USD per 1 million tokens
                  </th>
                  <th className="px-3 py-2 text-left font-medium">
                    Output: USD per 1 million tokens
                  </th>
                  <th className="px-3 py-2 text-left font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {provider.models.map((model) => (
                  <tr key={model.id} className="border-t">
                    <td className="px-3 py-2 font-medium break-words">
                      {model.id}
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
              onClick={() => onAction("toggle-enabled", provider)}
              disabled={busy}
            >
              {provider.enabled === false
                ? "Enable provider"
                : "Disable provider"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing(editing === "models" ? null : "models")}
            >
              Edit models
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing(editing === "caps" ? null : "caps")}
            >
              Edit caps
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setEditing(editing === "plans" ? null : "plans")}
            >
              Edit plans
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onAction("test", provider)}
            >
              <RefreshCw aria-hidden="true" /> Check provider connection
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`More actions for ${provider.name}`}
                >
                  <MoreHorizontal aria-hidden="true" /> More
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {provider.keySource === "env" ? (
                  <DropdownMenuItem disabled>
                    Key owned by {provider.apiKeyEnv ?? "environment"}
                  </DropdownMenuItem>
                ) : (
                  <>
                    <DropdownMenuItem
                      onClick={() => onAction("rotate", provider)}
                    >
                      <RotateCw aria-hidden="true" /> Rotate key
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => onAction("delete-key", provider)}
                    >
                      <KeyRound aria-hidden="true" /> Delete key
                    </DropdownMenuItem>
                  </>
                )}
                <DropdownMenuItem
                  variant={
                    provider.needsReconciliation ? undefined : "destructive"
                  }
                  onClick={() =>
                    provider.needsReconciliation
                      ? onAction("reconcile", provider)
                      : onAction("remove", provider)
                  }
                >
                  {provider.needsReconciliation ? (
                    <>
                      <MinusCircle aria-hidden="true" /> Reconcile before
                      removing
                    </>
                  ) : (
                    <>
                      <Trash2 aria-hidden="true" /> Remove provider
                    </>
                  )}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          {editing === "models" && (
            <div className="mt-4 space-y-3 rounded-lg border p-3">
              <h3 className="text-sm font-medium">Edit models and prices</h3>
              {modelDraft.map((model) => (
                <div
                  key={model.id}
                  className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_110px_110px_auto]"
                >
                  <label className="flex items-start gap-3 text-sm">
                    <Checkbox
                      checked={model.enabled}
                      onCheckedChange={(checked) =>
                        updateModel(model.id, { enabled: checked === true })
                      }
                      aria-label={`Enable ${model.id}`}
                    />
                    <span>
                      <span className="block font-medium">{model.id}</span>
                      <span className="text-muted-foreground break-words">
                        {model.upstreamModel}
                      </span>
                    </span>
                  </label>
                  <Input
                    aria-label={`${model.id} managed input price`}
                    value={model.priceInput}
                    onChange={(event) =>
                      updateModel(model.id, {
                        priceInput: event.target.value,
                      })
                    }
                    placeholder="USD per 1M input tokens"
                  />
                  <Input
                    aria-label={`${model.id} managed output price`}
                    value={model.priceOutput}
                    onChange={(event) =>
                      updateModel(model.id, {
                        priceOutput: event.target.value,
                      })
                    }
                    placeholder="USD per 1M output tokens"
                  />
                  <Button
                    size="sm"
                    onClick={() => onSaveModels(provider, modelDraft)}
                    disabled={busy}
                  >
                    Save models
                  </Button>
                </div>
              ))}
            </div>
          )}
          {editing === "caps" && (
            <div className="mt-4 grid gap-3 rounded-lg border p-3 sm:grid-cols-[1fr_1fr_auto]">
              <label className="space-y-2 text-sm font-medium">
                Daily cap (USD){" "}
                <Input
                  aria-label={`${provider.name} daily cap (USD)`}
                  value={capsDraft.daily}
                  onChange={(event) =>
                    setCapsDraft((draft) => ({
                      ...draft,
                      daily: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="space-y-2 text-sm font-medium">
                Monthly cap (USD){" "}
                <Input
                  aria-label={`${provider.name} monthly cap (USD)`}
                  value={capsDraft.monthly}
                  onChange={(event) =>
                    setCapsDraft((draft) => ({
                      ...draft,
                      monthly: event.target.value,
                    }))
                  }
                />
              </label>
              <Button
                className="self-end"
                size="sm"
                onClick={() =>
                  onSaveCaps(provider, capsDraft.daily, capsDraft.monthly)
                }
                disabled={busy}
              >
                Save caps
              </Button>
            </div>
          )}
          {editing === "plans" && (
            <div className="mt-4 space-y-3 rounded-lg border p-3">
              <h3 className="text-sm font-medium">Edit access plans</h3>
              {planDraft.length === 0 && (
                <p className="text-muted-foreground text-sm">
                  No plans selected. Paid requests will be denied.
                </p>
              )}
              {plans.map((plan) => (
                <label
                  key={plan.id}
                  className="flex items-center justify-between gap-3 rounded-lg border p-3 text-sm"
                >
                  <span>{plan.name}</span>
                  <Switch
                    checked={planDraft.includes(plan.id)}
                    onCheckedChange={(checked) =>
                      setPlanDraft((rows) =>
                        checked
                          ? [...new Set([...rows, plan.id])]
                          : rows.filter((id) => id !== plan.id),
                      )
                    }
                    aria-label={`Allow ${plan.name} for ${provider.name}`}
                  />
                </label>
              ))}
              <Button
                size="sm"
                onClick={() => onSavePlans(provider, planDraft)}
                disabled={busy}
              >
                Save plans
              </Button>
            </div>
          )}
        </div>
      </details>
    </article>
  );
}

function ReconciliationDialog({
  provider,
  open,
  onOpenChange,
}: {
  provider: ProviderRecord | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [amounts, setAmounts] = useState<Record<number, string>>({});
  const encoded = provider ? encodeURIComponent(provider.id) : "";
  const { data, error, isError, isFetching, refetch } = useQuery({
    queryKey: ["providers", provider?.id, "reconciliation"],
    queryFn: () =>
      adminFetch<unknown>(
        `/admin/api/providers/${encoded}/reconciliation`,
      ).then(mapReconciliations),
    enabled: open && provider !== null,
    retry: false,
  });
  const settle = useMutation({
    mutationFn: ({ id, actualUsd }: { id: number; actualUsd: string }) =>
      adminFetch(`/admin/api/providers/${encoded}/reconciliation/${id}`, {
        method: "POST",
        body: { actual_usd: actualUsd.trim() },
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      await refetch();
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      await refetch();
    },
  });
  const rows = data?.reconciliations ?? [];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Reconcile provider charges</DialogTitle>
          <DialogDescription>
            Record verified charges from {provider?.name ?? "the provider"}.
            Estimates are shown for matching only; they are not final bills.
          </DialogDescription>
        </DialogHeader>
        {isFetching ? (
          <div role="status" className="text-muted-foreground text-sm">
            Loading reconciliation rows…
          </div>
        ) : isError ? (
          <div role="alert" className="text-danger text-sm">
            {error instanceof Error
              ? error.message
              : "Reconciliation rows could not be loaded."}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3 block"
              onClick={() => refetch()}
            >
              Refresh reconciliation
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            No pending provider charges need reconciliation.
          </p>
        ) : (
          <div className="space-y-4">
            {rows.map((row) => {
              const value = amounts[row.id] ?? "";
              const valid = decimalInputValid(value);
              return (
                <div key={row.id} className="rounded-lg border p-3">
                  <h3 className="text-sm font-medium">Charge #{row.id}</h3>
                  <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                    <div>
                      <span className="text-muted-foreground">Estimate</span>
                      <div className="font-medium">
                        {preciseUsd(row.estimatedUsdText, row.estimatedUsd)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Cost source</span>
                      <div className="font-medium">{row.costSource}</div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Request ID</span>
                      <div className="font-mono text-xs break-all">
                        {formatIdentity(row.requestId)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">
                        Generation ID
                      </span>
                      <div className="font-mono text-xs break-all">
                        {formatIdentity(row.generationId)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Access plan</span>
                      <div className="font-mono text-xs break-all">
                        {formatIdentity(row.profileId)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Created</span>
                      <div>
                        {row.createdAt == null ? (
                          "Not recorded"
                        ) : (
                          <RelativeTime ts={row.createdAt} />
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_auto]">
                    <label className="space-y-2 text-sm font-medium">
                      Verified actual charge in USD
                      <Input
                        inputMode="decimal"
                        aria-label={`Verified actual charge for reconciliation ${row.id}`}
                        placeholder="0.00"
                        value={value}
                        onChange={(event) =>
                          setAmounts((draft) => ({
                            ...draft,
                            [row.id]: event.target.value,
                          }))
                        }
                      />
                    </label>
                    <Button
                      className="self-end"
                      disabled={!valid || settle.isPending}
                      onClick={() =>
                        settle.mutate({ id: row.id, actualUsd: value })
                      }
                    >
                      Record actual
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {settle.error && (
          <div role="alert" className="text-danger text-sm">
            {settle.error instanceof Error
              ? settle.error.message
              : "Reconciliation failed."}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => refetch()}>
            Refresh
          </Button>
          <Button type="button" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
  const checkGeneration = useRef(0);
  const credentialOrigin = useRef("\u0000");

  function resetCheck() {
    checkGeneration.current += 1;
    setTestResult(null);
    setModels([]);
  }

  function clearCredentialsAndCheck() {
    setSecret("");
    setEnvName("");
    resetCheck();
  }

  function chooseProvider(provider: CatalogProvider) {
    setSelected(provider);
    setBaseUrl(provider.baseUrl === "Not supplied" ? "" : provider.baseUrl);
    clearCredentialsAndCheck();
    setStep("credentials");
  }

  function changeBaseUrl(value: string) {
    setBaseUrl(value);
    clearCredentialsAndCheck();
  }

  useEffect(() => {
    const origin = `${selected?.id ?? ""}\u0000${baseUrl}`;
    if (credentialOrigin.current === origin) return;
    credentialOrigin.current = origin;
    setSecret("");
    setEnvName("");
    resetCheck();
  }, [baseUrl, selected?.id]);

  function closeDialog(nextOpen: boolean) {
    if (!nextOpen) {
      setSecret("");
      setEnvName("");
      setTestResult(null);
      setModels([]);
      checkGeneration.current += 1;
      setStep("catalog");
    }
    onOpenChange(nextOpen);
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
      const generation = checkGeneration.current;
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
      if (generation === checkGeneration.current) {
        setTestResult(result);
        setModels(
          result.models.map((model) => ({
            ...model,
            enabled: model.priceKnown,
          })),
        );
      }
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
          models: modelPayload(models.filter((model) => model.enabled)),
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
      closeDialog(false);
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
    <Dialog open={open} onOpenChange={closeDialog}>
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
                  onClick={() => chooseProvider(provider)}
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
                  onChange={(e) => changeBaseUrl(e.target.value)}
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
                Check provider connection
              </Button>
              {testConnection.error && (
                <p role="alert" className="text-danger text-sm">
                  {testConnection.error instanceof Error
                    ? testConnection.error.message
                    : "Provider connection check failed. Check the provider settings and try again."}
                </p>
              )}
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
                description="Run Check provider connection to read models from the provider."
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
                      aria-label={`Enable ${model.id}`}
                    />
                    <span>
                      <span className="block font-medium">{model.id}</span>
                      <span className="text-muted-foreground text-sm">
                        {model.context || "Context not reported"}
                      </span>
                    </span>
                  </label>
                  <Input
                    aria-label={`${model.id} input price`}
                    value={model.priceInput}
                    onChange={(e) =>
                      price(model.id, "priceInput", e.target.value)
                    }
                    placeholder="USD per 1M input tokens"
                  />
                  <Input
                    aria-label={`${model.id} output price`}
                    value={model.priceOutput}
                    onChange={(e) =>
                      price(model.id, "priceOutput", e.target.value)
                    }
                    placeholder="USD per 1M output tokens"
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
                Daily cap (USD){" "}
                <Input
                  aria-label="Daily cap (USD)"
                  value={dailyCap}
                  onChange={(e) => setDailyCap(e.target.value)}
                />
              </label>
              <label className="space-y-2 text-sm font-medium">
                Monthly cap (USD){" "}
                <Input
                  aria-label="Monthly cap (USD)"
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
          <Button variant="outline" onClick={() => closeDialog(false)}>
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

function ProviderEmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="border-border bg-card rounded-xl border p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="bg-primary/10 text-primary relative flex size-12 shrink-0 items-center justify-center rounded-xl">
            <Fish className="size-9" aria-hidden="true" />
            <span className="bg-warning absolute top-3 -right-2 size-3 rounded-full" />
          </div>
          <div>
            <p className="font-medium">No providers yet</p>
            <p className="text-muted-foreground text-sm">
              Add one provider, then choose its models, access plans and USD
              caps before it can receive paid requests.
            </p>
          </div>
        </div>
        <Button onClick={onAdd}>
          <Plus aria-hidden="true" /> Add provider
        </Button>
      </div>
    </div>
  );
}

function GlobalCapsEditor({
  global,
  busy,
  onSave,
}: {
  global: GlobalProviderState;
  busy?: boolean;
  onSave: (daily: string, monthly: string) => void;
}) {
  const [daily, setDaily] = useState(
    global.capDaily == null ? "" : String(global.capDaily),
  );
  const [monthly, setMonthly] = useState(
    global.capMonthly == null ? "" : String(global.capMonthly),
  );
  return (
    <section className="rounded-xl border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-medium">Global spend cap</h2>
          <p className="text-muted-foreground mt-1 text-sm">
            {capSummary(global.capDaily, global.capMonthly)}
          </p>
        </div>
        <details className="group">
          <summary className="list-none marker:hidden">
            <span className="border-input bg-background hover:bg-accent hover:text-accent-foreground inline-flex h-8 cursor-pointer items-center justify-center rounded-md border px-3 text-sm font-medium transition-colors">
              Edit
            </span>
          </summary>
          <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <label className="space-y-2 text-sm font-medium">
              Daily cap (USD){" "}
              <Input
                aria-label="Global daily provider cap (USD)"
                value={daily}
                onChange={(event) => setDaily(event.target.value)}
              />
            </label>
            <label className="space-y-2 text-sm font-medium">
              Monthly cap (USD){" "}
              <Input
                aria-label="Global monthly provider cap (USD)"
                value={monthly}
                onChange={(event) => setMonthly(event.target.value)}
              />
            </label>
            <Button
              className="self-end"
              size="sm"
              onClick={() => onSave(daily, monthly)}
              disabled={busy}
            >
              Save global cap
            </Button>
          </div>
        </details>
      </div>
      {global.needsReconciliation && (
        <div className="border-warning/30 bg-warning/10 mt-4 rounded-lg border p-3 text-sm">
          <div className="font-medium">
            {reconciliationLabel(global.pendingReconciliationCount)}
          </div>
          <p className="text-muted-foreground mt-1">
            Provider totals include estimates. Final spend and remaining budget
            are unknown until an owner reconciles the matching receipt,
            provider, and reservation IDs.
          </p>
        </div>
      )}
    </section>
  );
}

export default function ProvidersPage() {
  const [addOpen, setAddOpen] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<{
    action: "delete-key" | "remove";
    provider: ProviderRecord;
  } | null>(null);
  const [reconcileTarget, setReconcileTarget] = useState<ProviderRecord | null>(
    null,
  );
  const queryClient = useQueryClient();
  const {
    data: providersData,
    error: providersError,
    isError: providersIsError,
    isPending: providersIsPending,
    refetch: refetchProviders,
  } = useQuery({
    queryKey: ["providers"],
    queryFn: () => jsonFetch("/admin/api/providers"),
    retry: false,
  });
  const { data: catalogData, isPending: catalogIsPending } = useQuery({
    queryKey: ["providers", "catalog"],
    queryFn: () => jsonFetch("/admin/api/providers/-/catalog").then(mapCatalog),
    retry: false,
  });
  const { data: plansData, isPending: plansIsPending } = useQuery({
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
        if (provider.needsReconciliation) {
          throw new Error(
            "Reconcile pending provider charges before removing this provider.",
          );
        }
        return adminFetch(`/admin/api/providers/${encoded}`, {
          method: "DELETE",
          body: { expected_generation: null },
        });
      }
      if (name === "toggle-enabled") {
        return adminFetch(`/admin/api/operator/services/${encoded}/enabled`, {
          method: "POST",
          body: { enabled: provider.enabled === false },
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
  const saveModels = useMutation({
    mutationFn: ({
      provider,
      models,
    }: {
      provider: ProviderRecord;
      models: ProviderModel[];
    }) =>
      adminFetch(
        `/admin/api/providers/${encodeURIComponent(provider.id)}/models`,
        {
          method: "PUT",
          body: { models: modelPayload(models), expected_generation: null },
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });
  const saveCaps = useMutation({
    mutationFn: ({
      provider,
      daily,
      monthly,
    }: {
      provider: ProviderRecord;
      daily: string;
      monthly: string;
    }) =>
      adminFetch(
        `/admin/api/providers/${encodeURIComponent(provider.id)}/caps`,
        {
          method: "PUT",
          body: { daily_usd: Number(daily), monthly_usd: Number(monthly) },
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });
  const savePlans = useMutation({
    mutationFn: ({
      provider,
      allowedPlans,
    }: {
      provider: ProviderRecord;
      allowedPlans: string[];
    }) =>
      adminFetch(
        `/admin/api/providers/${encodeURIComponent(provider.id)}/allowed-plans`,
        {
          method: "PUT",
          body: { allowed_plans: allowedPlans },
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });
  const saveGlobalCaps = useMutation({
    mutationFn: ({ daily, monthly }: { daily: string; monthly: string }) =>
      adminFetch("/admin/api/providers/-/global-caps", {
        method: "PUT",
        body: { daily_usd: Number(daily), monthly_usd: Number(monthly) },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });

  const loading = providersIsPending || catalogIsPending || plansIsPending;
  const missingApi =
    providersError instanceof Error &&
    /404|not found/i.test(providersError.message);
  const records = mapProviders(providersData);
  const globalState = mapGlobal(providersData);
  const catalogRows = catalogData ?? [];
  const planRows = plansData ?? [];
  const busy =
    action.isPending ||
    saveModels.isPending ||
    saveCaps.isPending ||
    savePlans.isPending ||
    saveGlobalCaps.isPending;

  return (
    <>
      <PageHeader
        title="Providers"
        answer="Remote providers are explicit paid routes."
        description="Requests only go remote when a model names that provider. Paid access is never automatic."
        actions={
          missingApi ? undefined : (
            <Button onClick={() => setAddOpen(true)}>
              <Plus aria-hidden="true" /> Add provider
            </Button>
          )
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
        ) : providersIsError ? (
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-lg border p-4 text-sm"
          >
            <p>
              {providersError instanceof Error
                ? providersError.message
                : "Provider status failed."}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => refetchProviders()}
            >
              <RefreshCw aria-hidden="true" /> Retry providers
            </Button>
          </div>
        ) : records.length === 0 && !globalState.needsReconciliation ? (
          <ProviderEmptyState onAdd={() => setAddOpen(true)} />
        ) : (
          <div className="space-y-5">
            <GlobalCapsEditor
              global={globalState}
              busy={busy}
              onSave={(daily, monthly) =>
                saveGlobalCaps.mutate({ daily, monthly })
              }
            />
            <div className="space-y-3">
              {records.map((provider) => (
                <DetailCard
                  key={provider.id}
                  provider={provider}
                  plans={planRows}
                  busy={busy}
                  onSaveModels={(providerRecord, models) =>
                    saveModels.mutate({ provider: providerRecord, models })
                  }
                  onSaveCaps={(providerRecord, daily, monthly) =>
                    saveCaps.mutate({
                      provider: providerRecord,
                      daily,
                      monthly,
                    })
                  }
                  onSavePlans={(providerRecord, allowedPlans) =>
                    savePlans.mutate({ provider: providerRecord, allowedPlans })
                  }
                  onAction={(name, providerRecord) =>
                    name === "reconcile"
                      ? setReconcileTarget(providerRecord)
                      : name === "delete-key" || name === "remove"
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
            {(action.error ||
              saveModels.error ||
              saveCaps.error ||
              savePlans.error ||
              saveGlobalCaps.error) && (
              <div role="alert" className="text-danger text-sm">
                <AlertCircle className="mr-1 inline size-4" />
                {(action.error ||
                  saveModels.error ||
                  saveCaps.error ||
                  savePlans.error ||
                  saveGlobalCaps.error) instanceof Error
                  ? (
                      action.error ||
                      saveModels.error ||
                      saveCaps.error ||
                      savePlans.error ||
                      saveGlobalCaps.error
                    )?.message
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
      <ReconciliationDialog
        provider={reconcileTarget}
        open={reconcileTarget !== null}
        onOpenChange={(open) => !open && setReconcileTarget(null)}
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
