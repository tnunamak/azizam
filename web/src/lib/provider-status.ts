"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

export type ProviderTone = "ok" | "warn" | "bad" | "neutral";
export type ProviderKeyState = "set" | "missing" | "unknown";

export interface ProviderStatusView {
  id: string;
  name: string;
  enabled: boolean | null;
  reachable: boolean | null;
  keyState: ProviderKeyState;
  keySuffix: string | null;
  spendText: string;
  tone: ProviderTone;
  /** Recorded spend this month in USD; null when the gateway has no record. */
  monthlySpend?: number | null;
  monthlyCap?: number | null;
  /** When the provider connection was last checked; null when never. */
  checkedAt?: number | null;
}

export type OptionalProviderStatus =
  | {
      state: "hidden";
      providers: [];
      raw: null;
      message: string;
      refetch: () => void;
    }
  | {
      state: "loading";
      providers: [];
      raw: null;
      message: string;
      refetch: () => void;
    }
  | {
      state: "error";
      providers: [];
      raw: null;
      message: string;
      refetch: () => void;
    }
  | {
      state: "ready";
      providers: ProviderStatusView[];
      raw: { providers: ProviderStatusView[] };
      message: string;
      refetch: () => void;
    };

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function money(value: unknown): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return `$${value.toFixed(2)}`;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function normalizedSpend(obj: Record<string, unknown>): string {
  const spend = objectValue(obj.spend);
  if (spend) {
    if (spend.status === "unknown") return "Not measured yet";

    const caps = objectValue(obj.caps);
    const monthly = money(spend.monthly_usd);
    if (monthly !== null) {
      const cap = money(caps?.monthly_usd);
      return cap ? `${monthly} of ${cap} this month` : `${monthly} this month`;
    }

    const daily = money(spend.daily_usd);
    if (daily !== null) {
      const cap = money(caps?.daily_usd);
      return cap ? `${daily} of ${cap} today` : `${daily} today`;
    }
    return "Not measured yet";
  }

  const amount = money(obj.spend_usd) ?? money(obj.current_spend_usd);
  const cap = money(obj.cap_usd) ?? money(obj.spend_cap_usd);
  return cap
    ? `${amount ?? "Not measured yet"} of ${cap}`
    : (amount ?? "Not measured yet");
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function spendNumber(obj: Record<string, unknown>, field: string): number | null {
  const spend = objectValue(obj.spend);
  if (!spend || spend.status === "unknown") return null;
  return numberOrNull(spend[field]);
}

function keyStateValue(obj: Record<string, unknown>): ProviderKeyState {
  const keyStatus = objectValue(obj.key_status);
  if (keyStatus) {
    return keyStatus.state === "set" || keyStatus.state === "missing"
      ? keyStatus.state
      : "unknown";
  }
  const keySet = bool(obj.key_set);
  return keySet === true ? "set" : keySet === false ? "missing" : "unknown";
}

function records(raw: unknown): unknown[] {
  if (raw == null) return [];
  if (Array.isArray(raw)) return raw;
  if (typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    if (Array.isArray(obj.providers)) return obj.providers;
  }
  return [];
}

function assertProviderPayload(
  raw: unknown,
): asserts raw is unknown[] | { providers: unknown[] } {
  if (Array.isArray(raw)) return;
  if (
    raw &&
    typeof raw === "object" &&
    Array.isArray((raw as Record<string, unknown>).providers)
  )
    return;
  throw new Error("Provider status returned an unsupported response.");
}

export function providerViews(raw: unknown): ProviderStatusView[] {
  return records(raw).map((item, index) => {
    const obj =
      item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    const id =
      stringValue(obj.id) ??
      stringValue(obj.provider_id) ??
      `provider-${index}`;
    const name =
      [stringValue(obj.name), stringValue(obj.label)].find(
        (candidate) => candidate !== null && candidate !== id,
      ) ?? "Remote provider";
    const enabled = bool(obj.enabled);
    const reachable = bool(obj.reachable);
    const keyState = keyStateValue(obj);
    const keyStatus = objectValue(obj.key_status);
    const keySuffix = keyState === "set" ? stringValue(keyStatus?.suffix) : null;
    const tone: ProviderTone =
      enabled === true && (reachable === false || keyState === "missing")
        ? "bad"
        : enabled === true && reachable === true && keyState === "set"
          ? "ok"
          : "neutral";
    return {
      id,
      name,
      enabled,
      reachable,
      keyState,
      keySuffix,
      spendText: normalizedSpend(obj),
      tone,
      monthlySpend: spendNumber(obj, "monthly_usd"),
      monthlyCap: numberOrNull(objectValue(obj.caps)?.monthly_usd),
      checkedAt: numberOrNull(obj.checked_at),
    };
  });
}

export async function fetchProviderStatus(): Promise<
  unknown[] | { providers: unknown[] } | null
> {
  const response = await fetch("/admin/api/providers", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 404) return null;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      payload && typeof payload === "object"
        ? (payload as { error?: { message?: string } }).error?.message
        : null;
    throw new Error(message || `Provider status failed (${response.status})`);
  }
  assertProviderPayload(payload);
  return payload;
}

/** Unsupported/unused providers need less traffic; live providers stay at 30s. */
export function providerStatusInterval(
  data: unknown[] | { providers: unknown[] } | null | undefined,
): number {
  if (data === null) return 300_000;
  if (
    data !== undefined &&
    providerViews(data).every((provider) => provider.enabled === false)
  )
    return 60_000;
  return 30_000;
}

export function useOptionalProviderStatus(
  enabled = true,
): OptionalProviderStatus {
  const {
    data,
    error,
    isError,
    isPending,
    refetch: refetchQuery,
  } = useQuery({
    queryKey: ["status", "optional-providers"],
    queryFn: fetchProviderStatus,
    enabled,
    refetchInterval: (query) => providerStatusInterval(query.state.data),
  });
  const views = useMemo(() => providerViews(data), [data]);
  const refetch = () => {
    if (enabled) void refetchQuery();
  };
  if (!enabled)
    return {
      state: "hidden",
      providers: [],
      raw: null,
      message: "Provider status is disabled for this view.",
      refetch,
    };
  if (isPending)
    return {
      state: "loading",
      providers: [],
      raw: null,
      message: "Checking provider status.",
      refetch,
    };
  if (isError)
    return {
      state: "error",
      providers: [],
      raw: null,
      message:
        error instanceof Error ? error.message : "Provider status failed.",
      refetch,
    };
  if (data === null)
    return {
      state: "hidden",
      providers: [],
      raw: null,
      message: "Provider admin API is not available.",
      refetch,
    };
  return {
    state: "ready",
    providers: views,
    raw: { providers: views },
    message: "Provider admin API is available.",
    refetch,
  };
}
