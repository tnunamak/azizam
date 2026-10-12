"use client";

import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { engineShortName } from "@/lib/health-presentation";
import { useOptionalProviderStatus } from "@/lib/provider-status";
import {
  capabilityView,
  requestCapability,
  verdictFor,
  type CapabilityView,
  type EngineSignal,
  type RequestSignal,
  type Verdict,
} from "@/lib/capability-state";
import { homeApi, type HomeSnapshot } from "@/app/_home/home-api";
import { capabilityTiles } from "@/app/_home/presentation";
import { appPlumbingCapabilities } from "@/lib/terms";

/** People see these names (VOICE glossary); the gateway's own titles still match. */
const LABELS: Record<string, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  speech: "Voice",
  voice: "Voice",
  transcribe: "Transcription",
  music: "Music",
  sfx: "Sound effects",
  audio: "Audio",
  embed: "Embeddings",
  rerank: "Reranking",
  decide: "Decisions",
};

export function capabilityLabel(key: string, title?: string | null): string {
  return LABELS[key] ?? title ?? key;
}

export interface ActivityRecord {
  receipt?: {
    request_id?: string;
    status?: string;
    backend?: string;
    intent?: string;
    path?: string;
    remote?: boolean;
    warnings?: string[];
    app_profile?: string;
  };
  created_at?: number;
  person_label?: string;
  app_label?: string;
}

/** The newest household requests. The API pages by 50; counts say so. */
export const ACTIVITY_SAMPLE = 50;

export function useActivitySample(enabled: boolean) {
  return useQuery({
    queryKey: ["status", "recent-requests"],
    enabled,
    queryFn: async (): Promise<ActivityRecord[]> => {
      const response = await fetch(
        `/admin/api/activity/requests?limit=${ACTIVITY_SAMPLE}`,
        { credentials: "same-origin" },
      );
      if (!response.ok) throw new Error("Recent requests could not be loaded.");
      const payload = (await response.json()) as { records?: ActivityRecord[] };
      return payload.records ?? [];
    },
    retry: false,
  });
}

export function requestsFromActivity(records: ActivityRecord[]): RequestSignal[] {
  return records.flatMap((record) =>
    typeof record.created_at === "number"
      ? [
          {
            capability: requestCapability(record.receipt?.intent, record.receipt?.path),
            status: String(record.receipt?.status ?? ""),
            at: record.created_at,
            remote: record.receipt?.remote ?? null,
            warnings: record.receipt?.warnings ?? null,
          },
        ]
      : [],
  );
}

export function requestsFromHome(home: HomeSnapshot | undefined): RequestSignal[] {
  return (home?.receipts ?? []).flatMap((receipt) => {
    const raw = receipt as typeof receipt & {
      remote?: boolean;
      warnings?: string[];
    };
    const at = receipt.accepted_at_ms
      ? receipt.accepted_at_ms / 1000
      : (receipt._stored_at ?? null);
    return at == null
      ? []
      : [
          {
            capability: requestCapability(receipt.intent, receipt.path),
            status: String(receipt.status ?? ""),
            at,
            remote: raw.remote ?? null,
            warnings: raw.warnings ?? null,
          },
        ];
  });
}

export interface CapabilityViewsResult {
  home: HomeSnapshot | undefined;
  views: CapabilityView[];
  verdict: Verdict;
  requests: RequestSignal[];
  isPending: boolean;
  isError: boolean;
  /** The health data behind owner views failed to load; views fall back to the member model. */
  ownerHealthMissing: boolean;
  /** The owner's health check request itself failed. */
  ownerHealthError: boolean;
  refetch: () => void;
  isFetching: boolean;
}

/**
 * Every surface's capability states, for the person looking. Owners add the
 * engine, provider and household request evidence; members use their own.
 */
export function useCapabilityViews(owner: boolean): CapabilityViewsResult {
  const {
    data: homeData,
    isPending: homePending,
    isError: homeError,
    refetch: refetchHome,
    isFetching: homeFetching,
  } = useQuery({
    queryKey: ["account", "home"],
    queryFn: homeApi.home,
    // Readiness evidence expires after 15 minutes; refresh it while the page stays open.
    refetchInterval: 60_000,
  });
  const {
    data: statusData,
    isError: statusError,
    refetch: refetchStatus,
    isFetching: statusFetching,
  } = useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    enabled: owner,
    refetchInterval: 60_000,
  });
  const { data: registryData } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    enabled: owner,
  });
  const providers = useOptionalProviderStatus(owner);
  const activity = useActivitySample(owner);
  const home =
    homeData && homeData.authenticated
      ? (homeData as HomeSnapshot)
      : undefined;
  const now = Date.now() / 1000;
  const providerIds = new Set(
    providers.state === "ready" ? providers.providers.map((item) => item.id) : [],
  );
  const unreachable = new Set(
    providers.state === "ready"
      ? providers.providers
          .filter((item) => item.enabled === true && item.reachable === false)
          .map((item) => item.id)
      : [],
  );
  const health = statusData?.current_health;
  const backendChecks = new Map(
    (statusData?.backends ?? []).map((backend) => [
      backend.service_id ?? backend.key ?? "",
      (backend as { observed_at_s?: number | null }).observed_at_s ?? null,
    ]),
  );
  const engines: EngineSignal[] | undefined =
    owner && health?.observation_available
      ? health.services.map((service) => ({
          serviceId: service.service_id,
          name: engineShortName(registryData?.services[service.service_id], service),
          labels: service.capability_labels ?? (service.capability_title ? [service.capability_title] : []),
          enabled: service.enabled,
          observation: unreachable.has(service.service_id) ? "down" : service.observation,
          tone: unreachable.has(service.service_id) ? "bad" : service.tone,
          inUse: service.in_use ?? null,
          startsOnDemand: service.starts_on_demand === true,
          lastError: service.last_error,
          isProvider: providerIds.has(service.service_id),
          checkedAt: backendChecks.get(service.service_id) ?? null,
        }))
      : undefined;
  const requests = owner && activity.data ? requestsFromActivity(activity.data) : requestsFromHome(home);
  // A member's summary covers what they use; embeddings and decisions are
  // what their apps use behind the scenes, so they never read "Embeddings down".
  const rows = (
    home ? capabilityTiles(home.capabilities, home.aliases) : []
  ).filter(
    (row) => owner || !appPlumbingCapabilities.has(row.icon_key ?? row.capability),
  );
  const views = rows.map((row) => {
    const key = row.icon_key ?? row.capability;
    return capabilityView(
      {
        key,
        label: capabilityLabel(key, row.capability_title),
        matchLabels: [row.capability_title ?? "", LABELS[key] ?? ""].filter(Boolean),
        status: row.status,
        reason_code: row.reason_code,
        available: row.available,
        evidence: row.availability?.evidence ?? null,
      },
      { now, engines, requests },
    );
  });
  return {
    home,
    views,
    verdict: verdictFor(views),
    requests,
    isPending: homePending,
    isError: homeError,
    ownerHealthMissing: owner && (statusError || !health?.observation_available),
    ownerHealthError: owner && statusError,
    refetch: () => {
      void refetchHome();
      if (owner) {
        void refetchStatus();
        void activity.refetch();
        providers.refetch();
      }
    },
    isFetching: homeFetching || statusFetching,
  };
}
