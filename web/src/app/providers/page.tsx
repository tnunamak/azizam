"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  ExternalLink,
  KeyRound,
  Loader2,
  MinusCircle,
  MoreHorizontal,
  Plus,
  RefreshCw,
  RotateCw,
  Search,
  ShieldCheck,
  Trash2,
  XCircle,
} from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { RelativeTime } from "@/components/product/relative-time";
import { ListCard } from "@/components/product/list-card";
import { StatusPill } from "@/components/product/status-pill";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
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
import { adminFetch, ApiError } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/cn";

type CredentialMode = "key" | "env";
type Step = "catalog" | "credentials" | "models" | "limits";

const stepLabels: Record<Step, string> = {
  catalog: "Provider",
  credentials: "Key",
  models: "Models",
  limits: "Access and caps",
};

interface CatalogProvider {
  id: string;
  name: string;
  logoKey: string;
  baseUrl: string;
  docsUrl?: string;
  keyUrl?: string;
  recommendation?: string;
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
  /** The App the charge belongs to (the ledger records no plan). */
  appName: string;
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
    return "Cost not recorded";
  }
  if (value > 0 && value < 0.01) {
    return "Less than USD 0.01";
  }
  return `USD ${value.toFixed(2)}`;
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
  if (trimmed) return `USD ${trimmed}`;
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
  if (count == null || count === 0) return "Charges need checking";
  return `${count} charge${count === 1 ? " needs" : "s need"} checking`;
}

// Phone layout for the models table: cells stack, and a labelled cell shows
// its column name above its value (the header is screen-reader only there).
const phoneCell =
  "break-words whitespace-normal max-sm:block max-sm:h-auto max-sm:min-w-0 max-sm:px-0 max-sm:py-1 max-sm:text-left";
const phoneLabelled =
  "max-sm:before:text-muted-foreground max-sm:before:whitespace-normal max-sm:before:text-meta max-sm:before:block max-sm:before:content-[attr(data-label)]";

function decimalInputValid(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(trimmed)) {
    return false;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0;
}

function capInput(value: string): number | null {
  if (!value.trim()) return null;
  if (!decimalInputValid(value)) {
    throw new Error("Caps must be non-negative numbers or blank for no cap.");
  }
  return Number(value);
}

/** The gateway's cost sources, in the words an owner checks a bill with. */
function chargeKind(costSource: string): string {
  if (costSource === "provider-reported" || costSource === "owner-reported")
    return "Verified provider charge";
  if (costSource === "price-table" || costSource === "litellm")
    return "Estimated";
  return "Not recorded";
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

/** Both prices are finite non-negative decimals, so the model can be billed. */
function pricesValid(model: ProviderModel): boolean {
  return decimalInputValid(model.priceInput) && decimalInputValid(model.priceOutput);
}

const priceError = "Enter a price of 0 or more.";

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
    priceKnown: decimalInputValid(priceInput) && decimalInputValid(priceOutput),
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
    };
  });
}

function mapPlans(raw: unknown): AccessPlan[] {
  const obj =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const rows = Array.isArray(obj.access_plans) ? obj.access_plans : [];
  return rows.map((item, index) => {
    const row =
      item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    const doc =
      row.plan && typeof row.plan === "object"
        ? (row.plan as { paid_providers?: unknown })
        : {};
    return {
      id: text(row.id, `plan-${index}`),
      name: text(row.name, `Plan ${index + 1}`),
      description: text(row.summary, "") || undefined,
      paidAllowed:
        Array.isArray(doc.paid_providers) && doc.paid_providers.length > 0,
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
      // Read-only: each access plan's Paid providers decide this.
      plans: Array.isArray(row.access_plans)
        ? row.access_plans.map((v) =>
            String((v as { id?: unknown })?.id ?? v),
          )
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
        appName: text(row.app_name, "Unknown app"),
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
    // An unexplained status names no layer; the caller says what failed.
    throw new Error(message || "");
  }
  return body;
}

function Logo({ value }: { value: string }) {
  return (
    <span className="bg-muted text-muted-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-panel text-meta font-semibold">
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
    reason: "Provider connection not confirmed.",
  };
}

function ProviderKeySummary({ provider }: { provider: ProviderRecord }) {
  if (provider.keyStatus === "missing") return <>Missing key</>;
  if (provider.keyStatus !== "set") return <>Key status unknown</>;
  // The variable name is a host detail; Provider details shows it.
  const source =
    provider.keySource === "env"
      ? "Key configured"
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
    spent == null || cap == null
      ? null
      : cap > 0
        ? Math.min(100, Math.round((spent / cap) * 100))
        : spent > 0 ? 100 : 0;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-meta">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">
          {money(spent)}
          {cap != null && spent != null ? ` of ${money(cap)}` : ""}
        </span>
      </div>
      {pct == null ? (
        <p className="text-muted-foreground text-meta">
          {/* A blank cap means no cap (SE-22); unknown spend is already
              "Cost not recorded" above, so it is not repeated here. */}
          {cap == null ? "No cap set" : `Cap ${money(cap)}`}
        </p>
      ) : (
        <Progress value={pct} aria-label={`${label} spend`} />
      )}
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

function samePrice(a: string, b: string) {
  if (a === b) return true;
  if (!a.trim() || !b.trim()) return false;
  return Number(a) === Number(b);
}

type ModelField = "enabled" | "priceInput" | "priceOutput";
const modelFields: ModelField[] = ["enabled", "priceInput", "priceOutput"];
/** The owner's edit of one field, with the saved value it started from. */
type FieldEdits = {
  [F in ModelField]?: { value: ProviderModel[F]; base: ProviderModel[F] };
};

function sameModelField(
  field: ModelField,
  a: ProviderModel[ModelField],
  b: ProviderModel[ModelField],
) {
  return field === "enabled"
    ? a === b
    : samePrice(a as string, b as string);
}

/**
 * Three-way merge of one model row: the saved row, plus each field the owner
 * edited. A field the owner left alone follows the saved row. An edit the
 * saved row already holds is done. An edit whose saved value changed since
 * the owner started is a conflict: the edit stays shown, and saving waits.
 */
function resolveModel(
  server: ProviderModel,
  edits: FieldEdits | undefined,
): { row: ProviderModel; conflicts: ModelField[] } {
  const row = { ...server };
  const conflicts: ModelField[] = [];
  for (const field of modelFields) {
    const edit = edits?.[field];
    if (!edit || sameModelField(field, edit.value, server[field])) continue;
    (row as Record<ModelField, unknown>)[field] = edit.value;
    if (!sameModelField(field, edit.base, server[field])) conflicts.push(field);
  }
  row.priceKnown = pricesValid(row);
  return { row, conflicts };
}

/**
 * A form draft over a server value that can refresh while the form is open.
 * A clean draft follows the server. A dirty draft keeps the edit and reports
 * a conflict when the saved value changes underneath it; saving waits until
 * the owner picks the saved values or keeps their own.
 */
function useServerDraft<T>(server: T) {
  const serverKey = JSON.stringify(server);
  const [state, setState] = useState({
    draft: server,
    baselineKey: serverKey,
    latest: server,
    conflict: false,
  });
  useEffect(() => {
    setState((current) => {
      if (current.baselineKey === serverKey) {
        return current.conflict
          ? { ...current, latest: server, conflict: false }
          : current;
      }
      const draftKey = JSON.stringify(current.draft);
      if (draftKey === current.baselineKey || draftKey === serverKey) {
        return { draft: server, baselineKey: serverKey, latest: server, conflict: false };
      }
      return { ...current, latest: server, conflict: true };
    });
    // serverKey stands for server: a refetch builds a new but equal object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey]);
  return {
    draft: state.draft,
    conflict: state.conflict,
    setDraft: (update: (draft: T) => T) =>
      setState((current) => ({ ...current, draft: update(current.draft) })),
    useSaved: () =>
      setState((current) => ({
        draft: current.latest,
        baselineKey: JSON.stringify(current.latest),
        latest: current.latest,
        conflict: false,
      })),
    keepMine: () =>
      setState((current) => ({
        ...current,
        baselineKey: JSON.stringify(current.latest),
        conflict: false,
      })),
  };
}

function DraftConflict({
  onUseSaved,
  onKeepMine,
}: {
  onUseSaved: () => void;
  onKeepMine: () => void;
}) {
  return (
    <div
      role="alert"
      className="border-warning/30 bg-warning/10 rounded-panel border p-3 text-meta sm:col-span-full"
    >
      <p>These values changed on the gateway while you were editing.</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onUseSaved}>
          Use saved values
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onKeepMine}>
          Keep my changes
        </Button>
      </div>
    </div>
  );
}

/** The gateway's own reason, else the caller's; adminFetch's fallback for
 * an unexplained status ("PUT /admin/api/… failed (400)") is not shown. */
function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code === "unexpected_response")
    return fallback;
  return error instanceof Error && error.message ? error.message : fallback;
}

function DetailCard({
  provider,
  featured = false,
  plans,
  busy,
  error,
  onEdit,
  onAction,
  onSaveModels,
  onSaveCaps,
  readOnly = false,
}: {
  provider: ProviderRecord;
  /** An administrator who is not the owner may look, not change. */
  readOnly?: boolean;
  featured?: boolean;
  plans: AccessPlan[];
  busy?: boolean;
  /** The failure of the latest action submitted for this provider. */
  error?: string | null;
  /** Called on any draft edit, so an old failure does not outlive it. */
  onEdit: () => void;
  onAction: (action: string, provider: ProviderRecord) => void;
  onSaveModels: (provider: ProviderRecord, models: ProviderModel[]) => void;
  onSaveCaps: (
    provider: ProviderRecord,
    daily: string,
    monthly: string,
  ) => void;
}) {
  const state = providerState(provider);
  const enabledModels = provider.models.filter((model) => model.enabled);
  const [modelEdits, setModelEdits] = useState<Record<string, FieldEdits>>({});
  // Drop an edit once the saved row holds it (the owner's save landed).
  useEffect(() => {
    setModelEdits((all) => {
      const next: Record<string, FieldEdits> = {};
      for (const model of provider.models) {
        const kept: FieldEdits = {};
        for (const field of modelFields) {
          const edit = all[model.id]?.[field];
          if (edit && !sameModelField(field, edit.value, model[field]))
            Object.assign(kept, { [field]: edit });
        }
        if (Object.keys(kept).length) next[model.id] = kept;
      }
      return JSON.stringify(next) === JSON.stringify(all) ? all : next;
    });
  }, [provider.models]);
  const modelRows = provider.models.map((model) => ({
    model,
    ...resolveModel(model, modelEdits[model.id]),
  }));
  const caps = useServerDraft({
    daily: provider.capDaily == null ? "" : String(provider.capDaily),
    monthly: provider.capMonthly == null ? "" : String(provider.capMonthly),
  });
  const capsDraft = caps.draft;
  const [editing, setEditing] = useState<"models" | "caps" | null>(
    null,
  );
  const [modelPriceError, setModelPriceError] = useState<string | null>(null);
  function updateModel(id: string, patch: Partial<ProviderModel>) {
    onEdit();
    setModelPriceError(null);
    const server = provider.models.find((model) => model.id === id);
    if (!server) return;
    setModelEdits((all) => {
      const edits: FieldEdits = { ...all[id] };
      for (const field of modelFields) {
        if (!(field in patch)) continue;
        // Typing the saved value back is no edit: the field follows the
        // saved row again, so a later server change is not a conflict.
        if (sameModelField(field, patch[field] as ProviderModel[ModelField], server[field])) {
          delete edits[field];
          continue;
        }
        // The first edit of a field remembers the saved value it started from.
        Object.assign(edits, {
          [field]: { value: patch[field], base: edits[field]?.base ?? server[field] },
        });
      }
      return { ...all, [id]: edits };
    });
  }
  function resolveModelConflict(id: string, keepMine: boolean) {
    setModelEdits((all) => {
      const next = { ...all };
      const server = provider.models.find((model) => model.id === id);
      if (!keepMine || !server) {
        delete next[id];
        return next;
      }
      // Keep mine: the edits now start from the values the gateway holds.
      const edits: FieldEdits = {};
      for (const field of modelFields) {
        const edit = all[id]?.[field];
        if (edit) Object.assign(edits, { [field]: { ...edit, base: server[field] } });
      }
      next[id] = edits;
      return next;
    });
  }
  function setCapsDraft(update: (draft: typeof capsDraft) => typeof capsDraft) {
    onEdit();
    caps.setDraft(update);
  }
  const queryClient = useQueryClient();
  // The check's own result, shown on this provider; the HTTP status alone
  // does not mean the provider answered, so only ok: true confirms it.
  const check = useMutation({
    mutationFn: async () => {
      const raw = await adminFetch<unknown>(
        `/admin/api/providers/${encodeURIComponent(provider.id)}/test`,
        { method: "POST", body: {} },
      );
      const ok =
        raw && typeof raw === "object"
          ? bool((raw as Record<string, unknown>).ok)
          : null;
      if (ok !== true) throw new Error("");
      return true;
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });
  const resetCheck = check.reset;
  // A new key makes an earlier check result stale.
  useEffect(() => {
    resetCheck();
  }, [provider.keySuffix, provider.keyUpdatedAt, resetCheck]);
  const overview = (
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
          <p className="text-muted-foreground text-meta">
            Totals include estimated charges that are not final bills.
          </p>
        )}
      </div>
      <div className="rounded-panel border p-3">
        <h3 className="text-meta font-medium">Enabled models</h3>
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
            <span className="text-muted-foreground text-meta">
              No models enabled
            </span>
          )}
        </div>
        <h3 className="mt-4 text-meta font-medium">Access plans</h3>
        <div className="mt-3 flex flex-wrap gap-2">
          {plans
            .filter((plan) => provider.plans.includes(plan.id))
            .map((plan) => (
              <Badge key={plan.id} variant="secondary">
                {plan.name}
              </Badge>
            ))}
          {!provider.plans.length && (
            <span className="text-muted-foreground text-meta">
              No access plan includes {provider.name}.
            </span>
          )}
        </div>
      </div>
    </div>
  );
  return (
    <li className="min-w-0">
      {featured && (
        <div className="space-y-4 p-4 sm:p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <Logo value={provider.name} />
              <div className="min-w-0">
                <h2 className="text-body font-semibold break-words">{provider.name}</h2>
                <p className="text-muted-foreground mt-0.5 text-meta">
                  <ProviderKeySummary provider={provider} />
                </p>
              </div>
            </div>
            <StatusPill status={state.status} reason={state.reason} summary />
          </div>
          {overview}
        </div>
      )}
      <details className="group">
        <summary className="list-none marker:hidden">
          {featured ? (
            <span className="border-input bg-background hover:bg-accent hover:text-accent-foreground mx-4 mb-4 inline-flex h-8 cursor-pointer items-center justify-center rounded-control border px-3 text-meta font-medium transition-colors max-sm:min-h-11 sm:mx-6">
              Manage
            </span>
          ) : (
            <div className="grid min-h-(--row-2) cursor-pointer gap-3 px-4 py-1.5 max-sm:min-h-(--row-phone-2) sm:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-center">
              <div className="flex min-w-0 gap-3 sm:items-center">
                <Logo value={provider.name} />
                <div className="min-w-0">
                  <h2 className="truncate text-body font-medium">
                    {provider.name}
                  </h2>
                  <span className="text-muted-foreground mt-0.5 block truncate text-meta">
                    <ProviderKeySummary provider={provider} />
                  </span>
                </div>
              </div>
              <StatusPill status={state.status} reason={state.reason} summary />
              <div className="min-w-0 space-y-2">
                {/* Unknown spend shows as "Cost not recorded", never blank. */}
                <SpendBar
                  label="This month"
                  spent={provider.spendMonth}
                  cap={provider.capMonthly}
                />
                {provider.capDaily != null && (
                  <p className="text-muted-foreground text-meta">
                    Daily cap: {money(provider.capDaily)}
                  </p>
                )}
              </div>
              <span className="border-input bg-background hover:bg-accent hover:text-accent-foreground inline-flex h-8 max-sm:min-h-11 w-full cursor-pointer items-center justify-center gap-2 rounded-control border px-3 text-meta font-medium transition-colors">
                Manage
              </span>
            </div>
          )}
        </summary>
        <div className="border-t px-4 pt-4 pb-4 sm:px-5">
          {!featured && overview}
          <details className="mt-3 text-meta">
            <summary className="text-muted-foreground min-h-8 max-sm:min-h-11 cursor-pointer content-center">
              Provider details
            </summary>
            <div className="text-muted-foreground space-y-1 pb-3 text-meta break-all">
              <p>Provider ID: {provider.id}</p>
              {provider.keySource === "env" && provider.apiKeyEnv && (
                <p>Key variable: {provider.apiKeyEnv}</p>
              )}
              {provider.models.some(
                (model) => model.upstreamModel !== model.id,
              ) && (
                <p>
                  Provider model IDs:{" "}
                  {provider.models
                    .map((model) => model.upstreamModel)
                    .join(", ")}
                </p>
              )}
            </div>
          </details>
          {provider.recommendation && (
            <div className="bg-warning/10 border-warning/30 text-foreground mt-4 rounded-panel border p-3 text-meta">
              {provider.recommendation}
            </div>
          )}
          {provider.reachable === true && provider.checkedAt != null && (
            <p className="text-muted-foreground mt-3 text-meta">
              Reachability checked <RelativeTime ts={provider.checkedAt} />.
            </p>
          )}
          {provider.needsReconciliation && (
            <div className="border-warning/30 bg-warning/10 mt-4 rounded-panel border p-3 text-meta">
              <div className="font-medium">
                {reconciliationLabel(provider.pendingReconciliationCount)}
              </div>
              <p className="text-muted-foreground mt-1">
                Spend totals include estimates until you record the verified
                provider charges. Check charges before removing this provider.
              </p>
              {!readOnly && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => onAction("reconcile", provider)}
                >
                  Check charges
                </Button>
              )}
            </div>
          )}
          <div
            className="mt-5 max-w-full overflow-x-auto rounded-panel border"
            tabIndex={0}
            aria-label={`${provider.name} models`}
          >
            {/* On a phone each model becomes a stacked card: the name, then labelled
                values, so no column header has to fit 390 px. */}
            <Table className="min-w-0 table-fixed max-sm:block">
              <TableHeader className="max-sm:sr-only">
                <TableRow>
                  <TableHead className="whitespace-normal">Model</TableHead>
                  <TableHead numeric className="whitespace-normal">
                    Input: USD per 1 million tokens
                  </TableHead>
                  <TableHead numeric className="whitespace-normal">
                    Output: USD per 1 million tokens
                  </TableHead>
                  <TableHead className="whitespace-normal">State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="max-sm:block">
                {!provider.models.length && (
                  <TableRow className="max-sm:block">
                    <TableCell
                      colSpan={4}
                      className="text-muted-foreground whitespace-normal"
                    >
                      No models configured for this provider.
                    </TableCell>
                  </TableRow>
                )}
                {provider.models.map((model) => (
                  <TableRow
                    key={model.id}
                    className="max-sm:grid max-sm:grid-cols-2 max-sm:gap-x-3 max-sm:px-3 max-sm:py-2"
                  >
                    <TableCell
                      className={cn(
                        phoneCell,
                        "text-body font-medium max-sm:col-span-2",
                      )}
                    >
                      {model.id}
                    </TableCell>
                    <TableCell
                      numeric
                      data-label="Input, USD per 1M tokens"
                      className={cn(phoneCell, phoneLabelled)}
                    >
                      {model.priceInput || "Unknown"}
                    </TableCell>
                    <TableCell
                      numeric
                      data-label="Output, USD per 1M tokens"
                      className={cn(phoneCell, phoneLabelled)}
                    >
                      {model.priceOutput || "Unknown"}
                    </TableCell>
                    <TableCell
                      data-label="State"
                      className={cn(
                        phoneCell,
                        phoneLabelled,
                        "max-sm:col-span-2",
                      )}
                    >
                      {model.enabled ? "Enabled" : "Disabled"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {!readOnly && (
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
              onClick={() => check.mutate()}
              disabled={check.isPending}
              aria-busy={check.isPending || undefined}
            >
              {check.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCw aria-hidden="true" />
              )}{" "}
              {check.isPending ? "Checking…" : "Check provider connection"}
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
                    Key is set on the gateway host
                  </DropdownMenuItem>
                ) : (
                  <>
                    <DropdownMenuItem
                      onClick={() => onAction("replace-key", provider)}
                    >
                      <RotateCw aria-hidden="true" /> Replace key
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
                      <MinusCircle aria-hidden="true" /> Check charges before
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
          )}
          {check.isSuccess ? (
            <p
              role="status"
              className="text-success mt-3 inline-flex items-center gap-2 text-meta"
            >
              <CheckCircle2 className="size-4" aria-hidden="true" />
              Connection confirmed
            </p>
          ) : check.isError ? (
            <p
              role="alert"
              className="text-danger mt-3 inline-flex items-center gap-2 text-meta"
            >
              <XCircle className="size-4 shrink-0" aria-hidden="true" />
              Provider connection could not be confirmed.{" "}
              {errorText(check.error, "Try again.")}
            </p>
          ) : null}
          {error && (
            <p role="alert" className="text-danger mt-3 text-meta">
              <AlertCircle className="mr-1 inline size-4" aria-hidden="true" />
              {error}
            </p>
          )}
          {editing === "models" && (
            <div className="mt-4 space-y-3 rounded-panel border p-3">
              <h3 className="text-meta font-medium">Edit models and prices</h3>
              {!modelRows.length && (
                <p className="text-muted-foreground text-meta">
                  No models configured for this provider.
                </p>
              )}
              {modelPriceError && (
                <p role="alert" className="text-danger text-meta">
                  {modelPriceError}
                </p>
              )}
              {modelRows.map(({ row: model, conflicts }) => (
                <div
                  key={model.id}
                  className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
                >
                  <label className="flex items-start gap-3 text-meta">
                    <Checkbox
                      checked={model.enabled}
                      onCheckedChange={(checked) =>
                        updateModel(model.id, { enabled: checked === true })
                      }
                      aria-label={`Enable ${model.id}`}
                    />
                    <span className="font-medium break-all">{model.id}</span>
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
                    onClick={() => {
                      // Blank prices mean unpriced; anything else must be a
                      // price, not silently sent as "no price".
                      if (
                        [model.priceInput, model.priceOutput].some(
                          (value) => value.trim() && !decimalInputValid(value),
                        )
                      ) {
                        setModelPriceError(priceError);
                        return;
                      }
                      onSaveModels(
                        provider,
                        // The server replaces the whole list: send saved rows
                        // with only this row's draft.
                        provider.models.map((saved) =>
                          saved.id === model.id ? model : saved,
                        ),
                      );
                    }}
                    disabled={busy || conflicts.length > 0}
                  >
                    Save models
                  </Button>
                  {conflicts.length > 0 && (
                    <DraftConflict
                      onUseSaved={() => resolveModelConflict(model.id, false)}
                      onKeepMine={() => resolveModelConflict(model.id, true)}
                    />
                  )}
                </div>
              ))}
            </div>
          )}
          {editing === "caps" && (
            <div className="mt-4 grid gap-3 rounded-panel border p-3 sm:grid-cols-[1fr_1fr_auto]">
              <label className="space-y-2 text-meta font-medium">
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
              <label className="space-y-2 text-meta font-medium">
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
                disabled={busy || caps.conflict}
              >
                Save caps
              </Button>
              {caps.conflict && (
                <DraftConflict
                  onUseSaved={caps.useSaved}
                  onKeepMine={caps.keepMine}
                />
              )}
            </div>
          )}
        </div>
      </details>
    </li>
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
          <DialogTitle>Check provider charges</DialogTitle>
          <DialogDescription>
            Record verified charges from {provider?.name ?? "the provider"}.
            Estimates are shown for matching only; they are not final bills.
          </DialogDescription>
        </DialogHeader>
        {isFetching ? (
          <div role="status" className="text-muted-foreground text-meta">
            Loading charges…
          </div>
        ) : isError ? (
          <div role="alert" className="text-danger text-meta">
            {error instanceof Error
              ? error.message
              : "Charges could not be loaded. Try again."}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3 block"
              onClick={() => refetch()}
            >
              Refresh charges
            </Button>
          </div>
        ) : rows.length === 0 ? (
          <p className="text-muted-foreground text-meta">
            No charges need checking.
          </p>
        ) : (
          <div className="space-y-4">
            {rows.map((row) => {
              const value = amounts[row.id] ?? "";
              const valid = decimalInputValid(value);
              return (
                <div key={row.id} className="rounded-panel border p-3">
                  <h3 className="text-meta font-medium">Charge #{row.id}</h3>
                  <div className="mt-3 grid gap-2 text-meta sm:grid-cols-2">
                    <div>
                      <span className="text-muted-foreground">Estimate</span>
                      <div className="font-medium">
                        {preciseUsd(row.estimatedUsdText, row.estimatedUsd)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Charge</span>
                      <div className="font-medium">
                        {chargeKind(row.costSource)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Request ID</span>
                      <div className="font-mono text-meta break-all">
                        {formatIdentity(row.requestId)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">
                        Generation ID
                      </span>
                      <div className="font-mono text-meta break-all">
                        {formatIdentity(row.generationId)}
                      </div>
                    </div>
                    <div>
                      <span className="text-muted-foreground">App</span>
                      <div className="break-all">
                        {row.appName}
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
                    <label className="space-y-2 text-meta font-medium">
                      Verified actual charge in USD
                      <Input
                        inputMode="decimal"
                        aria-label={`Verified actual charge for charge ${row.id}`}
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
          <div role="alert" className="text-danger text-meta">
            {settle.error instanceof Error
              ? settle.error.message
              : "The charge could not be recorded. Try again."}
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
  metadataLoading,
  metadataError,
  onRetry,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: CatalogProvider[];
  metadataLoading: boolean;
  metadataError: unknown;
  onRetry: () => void;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>("catalog");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<CatalogProvider | null>(null);
  const [mode, setMode] = useState<CredentialMode>("key");
  const [secret, setSecret] = useState("");
  const [envName, setEnvName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [dailyCap, setDailyCap] = useState("5");
  const [monthlyCap, setMonthlyCap] = useState("50");
  const checkGeneration = useRef(0);
  const credentialOrigin = useRef("\u0000");
  // Each open of the dialog is a session. A save belongs to the session that
  // started it: a late answer must not close or reset a newer session, and
  // resetting the mutation's observer is not cancelling its request.
  const [session, setSession] = useState(0);
  const sessionRef = useRef(0);
  const [savingSessions, setSavingSessions] = useState<number[]>([]);
  const saving = savingSessions.includes(session);

  function resetCheck() {
    checkGeneration.current += 1;
    setTestResult(null);
    setModels([]);
    testConnection.reset();
    saveProvider.reset();
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
    // Access is chosen per provider: a plan picked for another provider
    // must not carry over.
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
    // The handlers that change the origin also reset the mutations.
    checkGeneration.current += 1;
    setTestResult(null);
    setModels([]);
  }, [baseUrl, selected?.id]);

  // Every close starts the next add flow fresh: no key, plan, cap or
  // earlier failure survives a cancel.
  function closeDialog(nextOpen: boolean) {
    if (!nextOpen) {
      sessionRef.current += 1;
      setSession(sessionRef.current);
      setSearch("");
      setSelected(null);
      setMode("key");
      setSecret("");
      setEnvName("");
      setBaseUrl("");
      resetCheck();
      setDailyCap("5");
      setMonthlyCap("50");
        setStep("catalog");
    }
    onOpenChange(nextOpen);
  }

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return term
      ? catalog.filter((p) =>
          `${p.name} ${p.baseUrl}`.toLowerCase().includes(term),
        )
      : catalog;
  }, [catalog, search]);

  const testConnection = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error("Choose a provider first.");
      const generation = checkGeneration.current;
      setTestResult(null);
      setModels([]);
      const raw = await adminFetch<unknown>(
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
      const response =
        raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
      const ok = bool(response.ok);
      const result: TestResult = {
        ok: ok === true,
        message:
          ok === null
            ? "Connection not confirmed."
            : ok
              ? text(response.message, "Connection worked.")
              : `Connection failed${text(response.message, "") ? `: ${text(response.message, "")}` : "."}`,
        models: ok === true && Array.isArray(response.models)
          ? response.models.map(mapModel)
          : [],
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
    mutationFn: async (_sessionId: number) => {
      if (!selected) throw new Error("Choose a provider first.");
      if (models.some((model) => model.enabled && !pricesValid(model)))
        throw new Error(`${priceError} Each enabled model needs both prices.`);
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
            daily_usd: capInput(dailyCap),
            monthly_usd: capInput(monthlyCap),
          },
          expected_generation: null,
        },
      });
    },
    onSuccess: async (_data, sessionId) => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      if (sessionRef.current === sessionId) closeDialog(false);
    },
    onSettled: (_data, _error, sessionId) =>
      setSavingSessions((ids) => ids.filter((id) => id !== sessionId)),
  });

  function submitSave() {
    const sessionId = sessionRef.current;
    if (savingSessions.includes(sessionId)) return;
    setSavingSessions((ids) => [...ids, sessionId]);
    saveProvider.mutate(sessionId);
  }

  function toggleModel(id: string, enabled: boolean) {
    saveProvider.reset();
    setModels((rows) =>
      rows.map((row) => (row.id === id ? { ...row, enabled } : row)),
    );
  }

  function price(
    id: string,
    field: "priceInput" | "priceOutput",
    value: string,
  ) {
    saveProvider.reset();
    setModels((rows) =>
      rows.map((row) => (row.id === id ? { ...row, [field]: value } : row)),
    );
  }

  const canCredential =
    selected &&
    (baseUrl.trim() || selected.baseUrl) &&
    (mode === "env" ? envName.trim() : secret.trim());
  const canSave =
    selected && testResult?.ok === true && models.some((model) => model.enabled);
  const metadataUnavailable = metadataLoading || Boolean(metadataError);

  return (
    <Dialog open={open} onOpenChange={closeDialog}>
      {/* One shrinkable column, so a long URL or name cannot widen the
          dialog past a phone screen. */}
      <DialogContent className="max-h-[92dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Add provider</DialogTitle>
          <DialogDescription>
            Connect a remote provider. Requests use it only when a model or
            alias names it.
          </DialogDescription>
        </DialogHeader>
        <div
          className="grid grid-cols-2 gap-2 sm:grid-cols-4"
          aria-label="Provider setup steps"
        >
          {(["catalog", "credentials", "models", "limits"] as Step[]).map(
            (item, index) => (
              <button
                key={item}
                type="button"
                onClick={() => setStep(item)}
                disabled={
                  metadataUnavailable ||
                  (item !== "catalog" && !selected) ||
                  ((item === "models" || item === "limits") &&
                    testResult?.ok !== true)
                }
                className={cn(
                  "rounded-panel border px-3 py-2 text-left text-meta",
                  step === item && "border-primary bg-primary/10 text-link",
                )}
              >
                {index + 1}. {stepLabels[item]}
              </button>
            ),
          )}
        </div>
        {metadataLoading ? (
          <div
            role="status"
            aria-label="Loading providers"
            aria-busy="true"
            className="space-y-4"
          >
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : metadataError ? (
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-panel border p-4 text-meta"
          >
            <p>
              {errorText(
                metadataError,
                "Provider controls could not be loaded. Try again.",
              )}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={onRetry}
            >
              <RefreshCw aria-hidden="true" /> Retry providers
            </Button>
          </div>
        ) : null}
        {!metadataUnavailable && step === "catalog" && (
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
            {!catalog.length ? (
              <p className="text-muted-foreground text-meta">
                No provider presets available. Add one to the gateway&apos;s
                provider catalog.
              </p>
            ) : !filtered.length ? (
              <div className="flex flex-wrap items-center gap-3 text-meta">
                <p className="text-muted-foreground">
                  No providers match this search.
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setSearch("")}
                >
                  Clear search
                </Button>
              </div>
            ) : null}
            <div className="grid gap-3 md:grid-cols-2">
              {filtered.map((provider) => (
                <button
                  key={provider.id}
                  type="button"
                  onClick={() => chooseProvider(provider)}
                  className={cn(
                    "hover:border-primary min-w-0 rounded-panel border p-4 text-left transition",
                    selected?.id === provider.id &&
                      "border-primary bg-primary/10",
                  )}
                >
                  <div className="flex gap-3">
                    <Logo value={provider.logoKey || provider.name} />
                    <div className="min-w-0">
                      <div className="font-medium">{provider.name}</div>
                      <div className="text-muted-foreground mt-1 text-meta [overflow-wrap:anywhere]">
                        {provider.baseUrl}
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
        {!metadataUnavailable && step === "credentials" && selected && (
          <div className="space-y-4">
            <div className="rounded-panel border p-4">
              <div className="font-medium">{selected.name}</div>
              <label className="mt-3 block space-y-2 text-meta font-medium">
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
                  className="text-link mt-2 inline-flex items-center gap-1 text-meta"
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
                placeholder="PROVIDER_KEY"
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
                <p role="alert" className="text-danger text-meta">
                  {testConnection.error instanceof Error
                    ? testConnection.error.message
                    : "Provider connection check failed. Check the provider settings and try again."}
                </p>
              )}
              {testResult && (
                <span
                  className={cn(
                    "inline-flex items-center gap-2 text-meta",
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
        {!metadataUnavailable && step === "models" && (
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
                  className="grid gap-3 rounded-panel border p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
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
                      <span className="text-muted-foreground text-meta">
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
                  <Badge variant={pricesValid(model) ? "secondary" : "outline"}>
                    {pricesValid(model) ? "Priced" : "Needs price"}
                  </Badge>
                </div>
              ))
            )}
          </div>
        )}
        {!metadataUnavailable && step === "limits" && (
          <div className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-2 text-meta font-medium">
                Daily cap (USD){" "}
                <Input
                  aria-label="Daily cap (USD)"
                  value={dailyCap}
                  onChange={(e) => {
                    saveProvider.reset();
                    setDailyCap(e.target.value);
                  }}
                />
              </label>
              <label className="space-y-2 text-meta font-medium">
                Monthly cap (USD){" "}
                <Input
                  aria-label="Monthly cap (USD)"
                  value={monthlyCap}
                  onChange={(e) => {
                    saveProvider.reset();
                    setMonthlyCap(e.target.value);
                  }}
                />
              </label>
            </div>
            <p className="text-muted-foreground rounded-panel border p-3 text-meta">
              No access plan includes a new provider. Add it to a plan&apos;s
              Paid providers in People → Access plans.
            </p>
            {saveProvider.error && saveProvider.variables === session && (
              <div role="alert" className="text-danger text-meta">
                {saveProvider.error instanceof Error
                  ? saveProvider.error.message
                  : "Save failed."}
              </div>
            )}
          </div>
        )}
        {/* The step's navigation stays in reach while a long step scrolls. */}
        <DialogFooter className="bg-background sticky -bottom-6 -mx-6 -mb-6 flex-row flex-wrap justify-end border-t px-6 py-4">
          <Button variant="outline" onClick={() => closeDialog(false)}>
            Cancel
          </Button>
          {step !== "catalog" && (
            <Button
              variant="outline"
              disabled={metadataUnavailable}
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
                metadataUnavailable ||
                (step === "catalog" && !selected) ||
                (step === "credentials" && testResult?.ok !== true)
              }
            >
              Next
            </Button>
          ) : (
            <Button
              onClick={submitSave}
              disabled={metadataUnavailable || !canSave || saving}
            >
              {saving ? (
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

function ProviderEmptyState({
  onAdd,
  readOnly = false,
}: {
  onAdd: () => void;
  readOnly?: boolean;
}) {
  return (
    <div className="border-border bg-card rounded-panel border p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="bg-primary/10 text-link relative flex size-12 shrink-0 items-center justify-center rounded-panel">
            <Cloud className="size-9" aria-hidden="true" />
            <span className="bg-warning absolute top-3 -right-2 size-3 rounded-full" />
          </div>
          <div>
            <p className="font-medium">No providers yet</p>
            <p className="text-muted-foreground text-meta">
              Add one provider, then choose its models, access plans and USD
              caps before it can receive paid requests.
            </p>
          </div>
        </div>
        {!readOnly && (
          <Button onClick={onAdd}>
            <Plus aria-hidden="true" /> Add provider
          </Button>
        )}
      </div>
    </div>
  );
}

function GlobalCapsEditor({
  global,
  busy,
  error,
  onEdit,
  onSave,
  readOnly = false,
}: {
  global: GlobalProviderState;
  readOnly?: boolean;
  busy?: boolean;
  error?: string | null;
  onEdit: () => void;
  onSave: (daily: string, monthly: string) => void;
}) {
  const caps = useServerDraft({
    daily: global.capDaily == null ? "" : String(global.capDaily),
    monthly: global.capMonthly == null ? "" : String(global.capMonthly),
  });
  const { daily, monthly } = caps.draft;
  const setDaily = (value: string) => {
    onEdit();
    caps.setDraft((draft) => ({ ...draft, daily: value }));
  };
  const setMonthly = (value: string) => {
    onEdit();
    caps.setDraft((draft) => ({ ...draft, monthly: value }));
  };
  return (
    <section className="rounded-panel border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-medium">Global spend cap</h2>
          <p className="text-muted-foreground mt-1 text-meta">
            {capSummary(global.capDaily, global.capMonthly)}
          </p>
        </div>
        {!readOnly && (
        <details className="group">
          <summary className="list-none marker:hidden">
            <span className="border-input bg-background hover:bg-accent hover:text-accent-foreground inline-flex h-8 max-sm:min-h-11 cursor-pointer items-center justify-center rounded-control border px-3 text-meta font-medium transition-colors">
              Edit
            </span>
          </summary>
          <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <label className="space-y-2 text-meta font-medium">
              Daily cap (USD){" "}
              <Input
                aria-label="Global daily provider cap (USD)"
                value={daily}
                onChange={(event) => setDaily(event.target.value)}
              />
            </label>
            <label className="space-y-2 text-meta font-medium">
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
              disabled={busy || caps.conflict}
            >
              Save global cap
            </Button>
            {caps.conflict && (
              <DraftConflict
                onUseSaved={caps.useSaved}
                onKeepMine={caps.keepMine}
              />
            )}
          </div>
        </details>
        )}
      </div>
      {error && (
        <p role="alert" className="text-danger mt-3 text-meta">
          <AlertCircle className="mr-1 inline size-4" aria-hidden="true" />
          {error}
        </p>
      )}
      {global.needsReconciliation && (
        <div className="border-warning/30 bg-warning/10 mt-4 rounded-panel border p-3 text-meta">
          <div className="font-medium">
            {reconciliationLabel(global.pendingReconciliationCount)}
          </div>
          <p className="text-muted-foreground mt-1">
            Final spend and remaining cap are unknown until you record the
            verified provider charges.
          </p>
        </div>
      )}
    </section>
  );
}

/**
 * Replace a saved provider key. The key is typed into a masked field, sent
 * once, and cleared whenever the dialog closes; it is never logged or shown.
 */
function ReplaceKeyDialog({
  provider,
  onOpenChange,
}: {
  provider: ProviderRecord | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [secret, setSecret] = useState("");
  const replace = useMutation({
    mutationFn: ({ id, key }: { id: string; key: string }) =>
      adminFetch(`/admin/api/providers/${encodeURIComponent(id)}/key`, {
        method: "PUT",
        body: { api_key: key },
      }),
    onSuccess: async () => {
      setSecret("");
      toast.success("Provider key replaced");
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      close();
    },
  });
  function close() {
    setSecret("");
    replace.reset();
    onOpenChange(false);
  }
  return (
    <Dialog
      open={provider !== null}
      onOpenChange={(open) => {
        if (!open && !replace.isPending) close();
      }}
    >
      <DialogContent>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!provider || !secret.trim()) return;
            replace.mutate({ id: provider.id, key: secret.trim() });
          }}
        >
          <DialogHeader>
            <DialogTitle>Replace key for {provider?.name}</DialogTitle>
            <DialogDescription>
              Requests use the new key as soon as you save it. The old key
              stops being used here; revoke it with the provider too.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label htmlFor="replacement-provider-key" className="text-label">
              New provider key
            </label>
            <Input
              id="replacement-provider-key"
              type="password"
              autoComplete="off"
              spellCheck={false}
              autoFocus
              value={secret}
              disabled={replace.isPending}
              onChange={(event) => {
                setSecret(event.target.value);
                replace.reset();
              }}
            />
          </div>
          {replace.isError && (
            <p role="alert" className="text-danger text-meta">
              {errorText(
                replace.error,
                "The key could not be replaced. Try again.",
              )}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={replace.isPending}
              onClick={close}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!secret.trim() || replace.isPending}
              aria-busy={replace.isPending || undefined}
            >
              {replace.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}
              {replace.isPending ? "Replacing…" : "Replace key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function ProvidersPage() {
  const viewer = useViewer();
  // Every provider write belongs to the owner; an administrator may only look.
  const readOnly = viewer.admin && viewer.role !== "owner";
  const [addOpen, setAddOpen] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState<{
    action: "delete-key" | "remove";
    provider: ProviderRecord;
  } | null>(null);
  const [reconcileTarget, setReconcileTarget] = useState<ProviderRecord | null>(
    null,
  );
  const [replaceKeyTarget, setReplaceKeyTarget] =
    useState<ProviderRecord | null>(null);
  const queryClient = useQueryClient();
  const {
    data: providersData,
    error: providersError,
    isError: providersIsError,
    isPending: providersIsPending,
  } = useQuery({
    queryKey: ["providers"],
    queryFn: () => jsonFetch("/admin/api/providers"),
    retry: false,
  });
  const {
    data: catalogData,
    error: catalogError,
    isError: catalogIsError,
    isPending: catalogIsPending,
  } = useQuery({
    queryKey: ["providers", "catalog"],
    queryFn: () => jsonFetch("/admin/api/providers/-/catalog").then(mapCatalog),
    retry: false,
  });
  const {
    data: plansData,
    error: plansError,
    isError: plansIsError,
    isPending: plansIsPending,
  } = useQuery({
    queryKey: ["providers", "plans"],
    queryFn: () => jsonFetch("/admin/api/access-plans").then(mapPlans),
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
      if (name === "delete-key") {
        return adminFetch(`/admin/api/providers/${encoded}/key`, {
          method: "DELETE",
          body: {},
        });
      }
      if (name === "remove") {
        if (provider.needsReconciliation) {
          throw new Error(
            "Check pending charges before removing this provider.",
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
          body: { daily_usd: capInput(daily), monthly_usd: capInput(monthly) },
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });
  const saveGlobalCaps = useMutation({
    mutationFn: ({ daily, monthly }: { daily: string; monthly: string }) =>
      adminFetch("/admin/api/providers/-/global-caps", {
        method: "PUT",
        body: { daily_usd: capInput(daily), monthly_usd: capInput(monthly) },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
  });

  const loading = providersIsPending || catalogIsPending || plansIsPending;
  const loadFailed = providersIsError || catalogIsError || plansIsError;
  const loadError = providersError ?? catalogError ?? plansError;
  const records = mapProviders(providersData);
  const globalState = mapGlobal(providersData);
  const catalogRows = catalogData ?? [];
  const planRows = plansData ?? [];
  const busy =
    action.isPending ||
    saveModels.isPending ||
    saveCaps.isPending ||
    saveGlobalCaps.isPending;

  // Only the most recently submitted action may show a failure, and only on
  // the provider it was for: a later success or another provider's action
  // replaces it, and so does editing a draft.
  const pageMutations = [
    { mutation: action, fallback: "The provider could not be updated. Try again." },
    { mutation: saveModels, fallback: "The models could not be saved. Try again." },
    { mutation: saveCaps, fallback: "The caps could not be saved. Try again." },
    { mutation: saveGlobalCaps, fallback: "The global cap could not be saved. Try again." },
  ];
  const latest = pageMutations
    .filter(({ mutation }) => mutation.submittedAt > 0)
    .sort((a, b) => b.mutation.submittedAt - a.mutation.submittedAt)[0];
  const latestError = latest?.mutation.isError
    ? errorText(latest.mutation.error, latest.fallback)
    : null;
  const latestVariables = latest?.mutation.variables as
    | { provider?: ProviderRecord }
    | undefined;
  const errorProviderId = latestVariables?.provider?.id ?? null;
  const clearErrors = () => {
    for (const { mutation } of pageMutations) {
      if (mutation.isError) mutation.reset();
    }
  };
  const globalCaps = (
    <GlobalCapsEditor
      key="global-caps"
      global={globalState}
      busy={busy}
      error={errorProviderId === null ? latestError : null}
      onEdit={clearErrors}
      readOnly={readOnly}
      onSave={(daily, monthly) =>
        saveGlobalCaps.mutate({ daily, monthly })
      }
    />
  );
  return (
    <>
      <PageHeader
        title="Providers"
        answer="Paid models run with providers."
        tight
        actions={
          readOnly ? undefined : (
            <Button onClick={() => setAddOpen(true)}>
              <Plus aria-hidden="true" /> Add provider
            </Button>
          )
        }
      >
        {/* PageHeader shows only one of answer and description. */}
        <p className="text-muted-foreground text-meta max-w-2xl">
          A request uses a provider only when its model names one.
        </p>
        {readOnly && (
          <p className="text-muted-foreground text-meta mt-1">
            Only the owner can change this.
          </p>
        )}
      </PageHeader>
      <div className="max-w-(--content-max) pb-20 sm:pb-0">
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
        ) : loadFailed ? (
          <div
            role="alert"
            className="border-danger/30 bg-danger/10 text-danger rounded-panel border p-4 text-meta"
          >
            <p>
              {errorText(
                loadError,
                "Provider controls could not be loaded. Try again.",
              )}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => queryClient.refetchQueries({ queryKey: ["providers"] })}
            >
              <RefreshCw aria-hidden="true" /> Retry providers
            </Button>
          </div>
        ) : records.length === 0 && !globalState.needsReconciliation ? (
          <ProviderEmptyState onAdd={() => setAddOpen(true)} readOnly={readOnly} />
        ) : (
          <div className="space-y-(--space-section)">
            {records.length !== 1 && globalCaps}
            <ListCard label="Providers">
              {records.map((provider) => (
                <DetailCard
                  key={provider.id}
                  provider={provider}
                  featured={records.length === 1}
                  plans={planRows}
                  readOnly={readOnly}
                  busy={busy}
                  error={
                    errorProviderId === provider.id ? latestError : null
                  }
                  onEdit={clearErrors}
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
                  onAction={(name, providerRecord) =>
                    name === "reconcile"
                      ? setReconcileTarget(providerRecord)
                      : name === "replace-key"
                        ? setReplaceKeyTarget(providerRecord)
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
            </ListCard>
            {records.length === 1 && globalCaps}
          </div>
        )}
      </div>
      <AddProviderDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        catalog={catalogRows}
        metadataLoading={catalogIsPending || plansIsPending}
        metadataError={catalogError ?? plansError}
        onRetry={() => queryClient.refetchQueries({ queryKey: ["providers"] })}
      />
      <ReplaceKeyDialog
        provider={replaceKeyTarget}
        onOpenChange={(open) => !open && setReplaceKeyTarget(null)}
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
            ? confirmTarget.action === "remove"
              ? `Remove ${confirmTarget.provider.name} from this gateway?`
              : `Delete the saved key for ${confirmTarget.provider.name}?`
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
