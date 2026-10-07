"use client";

import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import NextImage from "next/image";
import { useSearchParams } from "next/navigation";
import {
  useMutation,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AudioLines,
  CalendarDays,
  Copy,
  Download,
  ExternalLink,
  ImageIcon,
  Pause,
  Play,
  Search,
  Trash2,
  Video,
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/cn";
import { formatAbsoluteUTC } from "@/lib/format";
import { RelativeTime } from "@/components/product/relative-time";
import { PageHeader } from "@/components/product/page-header";
import { ErrorState } from "@/components/product/error-state";
import { EmptyState } from "@/components/product/empty-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
} from "@/components/ui/overflow-menu";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useViewer } from "@/components/shell/session";
import { FilterToggle } from "@/components/product/filter-toggle";
import { productApi } from "@/lib/product-api";

type ModelCatalog = Awaited<ReturnType<typeof productApi.models>>;

type MediaKind = "image" | "video" | "audio";
type DateFilter = "all" | "7d" | "30d" | "90d";

interface MediaAsset {
  asset_id: string;
  request_id?: string | null;
  kind: MediaKind;
  prompt: string | null;
  model: string;
  created_at: number | string | null;
  download_url?: string | null;
  workflow_download_url?: string | null;
  media_type?: string;
  width?: number | null;
  height?: number | null;
  generation_ms?: number | null;
  workflow?: string | Record<string, unknown> | null;
  seed?: string | number | null;
  duration_s?: number | null;
  poster_asset_id?: string | null;
  owner_name?: string | null;
  source?: { id?: string; kind: string; label: string };
  available?: boolean;
  unavailable_reason?: string | null;
}

interface MediaResponse {
  assets: MediaAsset[];
  next_cursor?: string | null;
  models?: string[];
  sources?: { id: string; label: string }[];
}

function isMediaKind(value: string): value is MediaKind {
  return value === "image" || value === "video" || value === "audio";
}

function kindIcon(kind: MediaKind) {
  if (kind === "video") return Video;
  if (kind === "audio") return AudioLines;
  return ImageIcon;
}

function kindLabel(kind: MediaKind) {
  return kind[0].toUpperCase() + kind.slice(1);
}

function assetTitle(asset: MediaAsset) {
  const prompt = typeof asset.prompt === "string" ? asset.prompt.trim() : "";
  return prompt || kindLabel(asset.kind);
}

function formatDuration(seconds: number) {
  if (typeof seconds !== "number") return null;
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  return `${minutes}:${remainingSeconds.toString().padStart(2, "0")}`;
}

function buildMediaPath(filters: {
  kind: string;
  model: string;
  date: DateFilter;
  search: string;
  owner: string;
  source: string;
}) {
  const params = new URLSearchParams({ limit: "40" });
  if (isMediaKind(filters.kind)) params.set("kind", filters.kind);
  if (filters.model !== "all") params.set("model", filters.model);
  if (filters.date !== "all") {
    const days = Number.parseInt(filters.date, 10);
    params.set(
      "start",
      new Date(Date.now() - days * 86400000).toISOString().slice(0, 10),
    );
  }
  if (filters.search.trim()) params.set("q", filters.search.trim());
  if (filters.owner === "everyone") params.set("scope", "everyone");
  if (filters.owner === "mine") params.set("scope", "me");
  if (filters.source !== "all") params.set("source", filters.source);
  return `/me/api/media?${params.toString()}`;
}

function hasActiveFilters(filters: {
  kind: string;
  model: string;
  date: DateFilter;
  search: string;
  owner: string;
  source: string;
}) {
  return (
    filters.kind !== "all" ||
    filters.model !== "all" ||
    filters.date !== "all" ||
    filters.search.trim() !== "" ||
    filters.owner !== "default" ||
    filters.source !== "all"
  );
}

async function mediaRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  const payload = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      payload?.error?.message ?? "Library data could not be loaded. Try again.",
    );
  return payload as T;
}

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

function usePreviewVisible() {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(
    typeof IntersectionObserver === "undefined",
  );
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, visible };
}

function UnavailablePreview({
  kind,
  reason,
  onRetry,
}: {
  kind: MediaKind;
  reason?: string | null;
  onRetry?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  const Icon = kindIcon(kind);
  return (
    <div className="bg-muted text-muted-foreground flex h-full min-h-32 flex-col items-center justify-center gap-3 p-4 text-center">
      <div role="img" aria-label={`${kindLabel(kind)} preview unavailable`}>
        <Icon aria-hidden="true" className="mx-auto size-8" />
      </div>
      <p className="mt-3 text-sm">
        {reason?.trim().replace(/[_-]+/g, " ") || "Preview unavailable"}
      </p>
      {onRetry ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="relative z-20"
          onClick={onRetry}
        >
          Retry
        </Button>
      ) : null}
    </div>
  );
}

async function prepareDownload(asset: MediaAsset, everyone: boolean) {
  const path = `/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=${everyone ? "everyone" : "me"}`;
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error("Download could not be prepared. Try again.");
  }
  const payload = (await response.json()) as { download_url?: string };
  if (!payload.download_url) {
    throw new Error("Download could not be prepared. Try again.");
  }
  const link = document.createElement("a");
  link.href = payload.download_url;
  link.download = assetTitle(asset);
  link.click();
}

function absolute(ts: MediaAsset["created_at"]) {
  return ts ? formatAbsoluteUTC(ts) : undefined;
}

function modelDisplayName(model: string, catalog?: ModelCatalog) {
  const row = [...(catalog?.aliases ?? []), ...(catalog?.models ?? [])].find(
    (item) => item.id === model,
  );
  return row?.title || row?.display_name || model;
}

function pauseOtherAudio(current: HTMLAudioElement) {
  current.ownerDocument.querySelectorAll("audio").forEach((audio) => {
    if (audio !== current) audio.pause();
  });
}

function MiniAudioPlayer({
  src,
  title,
  duration,
}: {
  src: string;
  title: string;
  duration?: number | null;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [measuredDuration, setMeasuredDuration] = useState<number | null>(
    duration ?? null,
  );
  const [error, setError] = useState<"media" | "playback" | null>(null);

  useEffect(() => {
    setIsPlaying(false);
    setMeasuredDuration(duration ?? null);
    setError(null);
    const audio = audioRef.current;
    if (audio) {
      const hadSource = audio.hasAttribute("src");
      if (hadSource) audio.pause();
      audio.removeAttribute("src");
      if (hadSource) audio.load();
    }
  }, [duration, src]);

  const label = isPlaying ? `Pause ${title}` : `Play ${title}`;
  const durationLabel =
    error === "media"
      ? "Audio unavailable"
      : error === "playback"
        ? "Playback blocked. Try again."
        : measuredDuration != null &&
            Number.isFinite(measuredDuration) &&
            measuredDuration >= 0
          ? formatDuration(measuredDuration)
          : null;

  return (
    <div
      className="bg-background/95 absolute right-3 bottom-3 left-3 z-20 flex min-h-12 items-center gap-3 rounded-lg border p-2 shadow-sm backdrop-blur"
      onClick={(event) => event.stopPropagation()}
    >
      <audio
        ref={audioRef}
        preload="none"
        onLoadedMetadata={(event) => {
          setError(null);
          setMeasuredDuration(event.currentTarget.duration);
        }}
        onPlay={(event) => {
          pauseOtherAudio(event.currentTarget);
          setError(null);
          setIsPlaying(true);
        }}
        onPause={() => setIsPlaying(false)}
        onEnded={() => setIsPlaying(false)}
        onError={() => {
          setIsPlaying(false);
          setError("media");
        }}
      />
      <Button
        type="button"
        size="icon"
        variant="secondary"
        className="size-11"
        aria-label={label}
        onClick={async () => {
          const audio = audioRef.current;
          if (!audio) return;
          if (isPlaying) {
            audio.pause();
            setIsPlaying(false);
            return;
          }
          if (audio.getAttribute("src") !== src) {
            audio.setAttribute("src", src);
          }
          if (error === "media") {
            audio.load();
          }
          try {
            await audio.play();
            setError(null);
            setIsPlaying(true);
          } catch {
            setIsPlaying(false);
            setError(audio.error ? "media" : "playback");
          }
        }}
      >
        {isPlaying ? <Pause aria-hidden /> : <Play aria-hidden />}
      </Button>
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium">Audio preview</p>
        {durationLabel ? (
          <p
            role={error ? "status" : undefined}
            className={cn(
              "text-muted-foreground text-xs",
              error === "media" && "text-destructive",
            )}
          >
            {durationLabel}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Preview({
  asset,
  large = false,
  everyone = false,
  visible = true,
}: {
  asset: MediaAsset;
  large?: boolean;
  everyone?: boolean;
  visible?: boolean;
}) {
  const src = asset.download_url ?? null;
  const Icon = kindIcon(asset.kind);
  const queryClient = useQueryClient();
  const [mediaFailed, setMediaFailed] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [retrySrc, setRetrySrc] = useState<string | null>(null);
  const [recovered, setRecovered] = useState(false);
  const {
    data: poster,
    isPending: posterPending,
    isError: posterLoadFailed,
    refetch: refetchPoster,
  } = useQuery({
    queryKey: ["library", "poster", asset.poster_asset_id, everyone],
    enabled: asset.kind === "video" && !!asset.poster_asset_id,
    queryFn: () =>
      mediaRequest<{ download_url: string }>(
        `/me/api/media/${encodeURIComponent(asset.poster_asset_id!)}/download?scope=${everyone ? "everyone" : "me"}`,
      ),
    retry: false,
  });

  useEffect(() => {
    setMediaFailed(false);
    setPosterFailed(false);
    setRetryKey(0);
    setRetrySrc(null);
    setRecovered(false);
  }, [asset.asset_id, src, asset.poster_asset_id]);

  const posterUrl = poster?.download_url;
  const effectivePosterUrl = posterFailed ? undefined : posterUrl;
  const currentSrc = retrySrc ?? src;
  const waitingForPoster =
    asset.kind === "video" && !!asset.poster_asset_id && posterPending;
  const posterUnavailable =
    asset.kind !== "video" ||
    !asset.poster_asset_id ||
    posterLoadFailed ||
    posterFailed ||
    !effectivePosterUrl;
  const mediaUnavailable =
    !currentSrc || mediaFailed || (asset.available === false && !recovered);
  const unavailable =
    (asset.available === false && !recovered) ||
    (large && mediaFailed) ||
    (asset.kind === "video"
      ? posterUnavailable && mediaUnavailable
      : mediaUnavailable);
  if (waitingForPoster && asset.available !== false)
    return (
      <div
        role="status"
        aria-label="Loading video poster"
        className="bg-muted h-full min-h-32"
      />
    );
  if (unavailable)
    return (
      <UnavailablePreview
        kind={asset.kind}
        reason={asset.unavailable_reason}
        onRetry={(event) => {
          event.stopPropagation();
          setMediaFailed(false);
          setPosterFailed(false);
          setRetryKey((value) => value + 1);
          if (asset.poster_asset_id) void refetchPoster();
          void mediaRequest<{ download_url?: string }>(
            `/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=${everyone ? "everyone" : "me"}`,
          ).then(
            (payload) => {
              if (payload?.download_url) {
                setRetrySrc(payload.download_url);
                setRecovered(true);
                setMediaFailed(false);
                setRetryKey((value) => value + 1);
              } else {
                setMediaFailed(true);
                setPosterFailed(true);
              }
            },
            () => {
              setMediaFailed(true);
              setPosterFailed(true);
            },
          );
          void queryClient.invalidateQueries({ queryKey: ["library"] });
        }}
      />
    );
  if (asset.kind === "image" && currentSrc)
    return (
      <NextImage
        key={`image-${asset.asset_id}-${retryKey}-${currentSrc}`}
        unoptimized
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={currentSrc}
        alt={assetTitle(asset)}
        onError={() => setMediaFailed(true)}
        className={cn(
          "h-full w-full",
          large ? "rounded-lg object-contain" : "object-cover",
        )}
      />
    );
  if (
    asset.kind === "video" &&
    currentSrc &&
    (!asset.poster_asset_id || !waitingForPoster) &&
    (large || posterUnavailable)
  )
    return (
      <video
        key={`video-${asset.asset_id}-${retryKey}`}
        src={large || visible ? currentSrc : undefined}
        poster={effectivePosterUrl}
        preload="none"
        autoPlay={!large && visible}
        muted={!large}
        playsInline
        onLoadedData={(event) => {
          if (!large) event.currentTarget.pause();
        }}
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (!large && Number.isFinite(video.duration) && video.duration > 0) {
            video.currentTime = Math.min(0.1, video.duration / 2);
          }
        }}
        onError={() => setMediaFailed(true)}
        className={cn(
          "h-full w-full",
          large ? "rounded-lg object-contain" : "object-cover",
        )}
        controls={large}
        aria-label={assetTitle(asset)}
      />
    );
  if (asset.kind === "video" && effectivePosterUrl)
    return (
      <NextImage
        unoptimized
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={effectivePosterUrl}
        alt={assetTitle(asset)}
        onError={() => setPosterFailed(true)}
        className="h-full w-full object-cover"
      />
    );
  if (asset.kind === "audio")
    return large && currentSrc ? (
      <audio
        controls
        src={currentSrc}
        preload="none"
        onError={() => setMediaFailed(true)}
        aria-label="Generated audio"
        onPlay={(event) => pauseOtherAudio(event.currentTarget)}
        className="w-full"
      />
    ) : (
      <div className="bg-muted text-muted-foreground flex h-full flex-col items-center justify-center gap-3 pb-12">
        <AudioLines className="size-12" aria-hidden />
        {!src && <p className="text-sm">Audio preview unavailable</p>}
      </div>
    );
  return (
    <div
      className={cn(
        "flex h-full items-center justify-center",
        "bg-muted text-muted-foreground",
      )}
      role="img"
      aria-label={`${kindLabel(asset.kind)} preview unavailable`}
    >
      <Icon className="size-8" aria-hidden />
    </div>
  );
}

function AssetCard({
  asset,
  onOpen,
  everyone,
  modelCatalog,
}: {
  asset: MediaAsset;
  onOpen: (asset: MediaAsset) => void;
  everyone: boolean;
  modelCatalog?: ModelCatalog;
}) {
  const Icon = kindIcon(asset.kind);
  const { ref: visibilityRef, visible } = usePreviewVisible();
  return (
    <article className="group bg-card hover:border-primary/50 relative min-w-0 overflow-hidden rounded-xl border transition">
      <button
        type="button"
        onClick={() => onOpen(asset)}
        className="focus-visible:ring-ring absolute inset-0 z-10 rounded-xl focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
        aria-label={`Open ${assetTitle(asset)}`}
      />
      <div ref={visibilityRef} className="bg-muted relative aspect-[4/3]">
        <Preview
          key={`preview-${asset.asset_id}-${asset.download_url}`}
          asset={asset}
          everyone={everyone}
          visible={visible}
        />
        <Badge
          variant="secondary"
          className="bg-background/90 absolute top-2 left-2 gap-1 backdrop-blur"
        >
          <Icon className="size-3" />
          {kindLabel(asset.kind)}
        </Badge>
        {asset.kind !== "audio" &&
        asset.duration_s != null &&
        Number.isFinite(asset.duration_s) ? (
          <Badge
            variant="secondary"
            className="bg-background/90 absolute top-2 right-2 tabular-nums"
          >
            {formatDuration(asset.duration_s)}
          </Badge>
        ) : null}
        {asset.kind === "audio" && asset.download_url && (
          <MiniAudioPlayer
            key={asset.download_url}
            src={asset.download_url}
            title={assetTitle(asset)}
            duration={asset.duration_s}
          />
        )}
      </div>
      <div className="space-y-1 p-3">
        {asset.prompt?.trim() ? (
          <p className="line-clamp-2 min-h-10 min-w-0 text-sm font-medium break-words">
            {asset.prompt.trim()}
          </p>
        ) : null}
        {asset.model || asset.created_at ? (
          <div className="text-muted-foreground flex min-w-0 items-center justify-between gap-2 text-xs">
            {asset.model && modelDisplayName(asset.model, modelCatalog) ? (
              <span className="min-w-0 truncate">
                {modelDisplayName(asset.model, modelCatalog)}
              </span>
            ) : null}
            {asset.created_at ? (
              <RelativeTime ts={asset.created_at} fallback="" />
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

function LibraryFilterControls({
  kind,
  setKind,
  search,
  setSearch,
  model,
  setModel,
  models,
  date,
  setDate,
  owner,
  setOwner,
  source,
  setSource,
  sources,
  instanceView,
  canChooseOwner,
  mobileLabels = false,
}: {
  kind: string;
  setKind: (value: string) => void;
  search: string;
  setSearch: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  models: string[];
  date: DateFilter;
  setDate: (value: DateFilter) => void;
  owner: string;
  setOwner: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
  sources: { id: string; label: string }[];
  instanceView: boolean;
  canChooseOwner: boolean;
  mobileLabels?: boolean;
}) {
  return (
    <>
      <div role="group" aria-label="Kind" className="flex flex-wrap gap-2">
        {[
          { value: "all", label: "All kinds" },
          { value: "image", label: "Images" },
          { value: "video", label: "Video" },
          { value: "audio", label: "Audio" },
        ].map((filter) => (
          <FilterToggle
            key={filter.value}
            pressed={kind === filter.value}
            onClick={() => setKind(filter.value)}
            aria-label={
              mobileLabels ? `Filter kind ${filter.label}` : undefined
            }
          >
            {filter.label}
          </FilterToggle>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="relative block min-w-0 basis-full sm:flex-1 sm:basis-48">
          <span className="sr-only">Search prompts</span>
          <Search
            className="text-muted-foreground pointer-events-none absolute top-3.5 left-3 size-4"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search prompts"
            className="pl-9"
          />
        </label>
        <Select value={model} onValueChange={setModel}>
          <SelectTrigger aria-label="Model" className="w-full sm:w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All models</SelectItem>
            {models.map((m) => (
              <SelectItem key={m} value={m}>
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={date}
          onValueChange={(value) => setDate(value as DateFilter)}
        >
          <SelectTrigger aria-label="Date" className="w-full sm:w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Any time</SelectItem>
            <SelectItem value="7d">Last 7 days</SelectItem>
            <SelectItem value="30d">Last 30 days</SelectItem>
            <SelectItem value="90d">Last 90 days</SelectItem>
          </SelectContent>
        </Select>
        {canChooseOwner ? (
          <Select
            value={owner === "default" ? "everyone" : owner}
            onValueChange={(value) => {
              setOwner(value);
              setSource("all");
            }}
          >
            <SelectTrigger
              aria-label={mobileLabels ? "Filter owner" : "Owner"}
              className="w-full sm:w-40"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="mine">Mine</SelectItem>
              <SelectItem value="everyone">Everyone&apos;s</SelectItem>
            </SelectContent>
          </Select>
        ) : null}
        {instanceView ? (
          <Select value={source} onValueChange={setSource}>
            <SelectTrigger aria-label="Source" className="w-full sm:w-52">
              <SelectValue placeholder="All sources" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All sources</SelectItem>
              {sources.map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>
    </>
  );
}

export default function LibraryPage() {
  const viewer = useViewer();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { data: modelCatalog } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  const [kind, setKind] = useState("all");
  const [model, setModel] = useState("all");
  const [date, setDate] = useState<DateFilter>("all");
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("default");
  const [source, setSource] = useState("all");
  const [selected, setSelected] = useState<MediaAsset | null>(null);
  const closedDeepLinkId = useRef<string | null>(null);
  const [confirming, setConfirming] = useState<MediaAsset | null>(null);
  const [downloadingAssetId, setDownloadingAssetId] = useState<string | null>(
    null,
  );
  const instanceView =
    ["owner", "superadmin"].includes(viewer.role) && owner !== "mine";
  const debouncedSearch = useDebouncedValue(search, 250);
  const filters = { kind, model, date, search: debouncedSearch, owner, source };
  const activeFilters = hasActiveFilters(filters);
  const mediaPath = buildMediaPath(filters);
  const deepLinkedAssetId = searchParams?.get("asset") ?? "";
  function clearFilters() {
    setKind("all");
    setModel("all");
    setDate("all");
    setSearch("");
    setOwner("default");
    setSource("all");
  }
  function openAsset(asset: MediaAsset) {
    closedDeepLinkId.current = null;
    setSelected(asset);
    const params = new URLSearchParams(window.location.search);
    params.set("asset", asset.asset_id);
    window.history.replaceState(null, "", `?${params.toString()}`);
  }
  function closeAsset() {
    if (selected) closedDeepLinkId.current = selected.asset_id;
    setSelected(null);
    const params = new URLSearchParams(window.location.search);
    params.delete("asset");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      query ? `?${query}` : window.location.pathname,
    );
  }
  const {
    data,
    isLoading,
    isError,
    isFetching,
    isFetchingNextPage,
    isFetchNextPageError,
    isPlaceholderData,
    hasNextPage,
    fetchNextPage,
    refetch,
  } = useInfiniteQuery({
    queryKey: ["library", filters],
    initialPageParam: "",
    placeholderData: (previous) => previous,
    queryFn: ({ pageParam, signal }) =>
      mediaRequest<MediaResponse>(
        mediaPath +
          (pageParam ? `&cursor=${encodeURIComponent(String(pageParam))}` : ""),
        { signal },
      ),
    getNextPageParam: (last: MediaResponse) => last.next_cursor ?? undefined,
    retry: false,
  });
  const lastLoadedPages = useRef<MediaResponse[]>([]);
  useEffect(() => {
    if (data && !isPlaceholderData) lastLoadedPages.current = data.pages;
  }, [data, isPlaceholderData]);
  const mediaPages = useMemo(
    () => data?.pages ?? (isError ? lastLoadedPages.current : []),
    [data?.pages, isError],
  );
  const assets = useMemo(
    () =>
      mediaPages
        .flatMap((page) => page.assets)
        .filter((item) => isMediaKind(item.kind)),
    [mediaPages],
  );
  const hasLoadedAssets = assets.length > 0;
  const updating = isFetching && isPlaceholderData && hasLoadedAssets;
  const showInitialError = isError && !hasLoadedAssets && !isFetchNextPageError;
  const showRetainedError =
    (isError || isFetchNextPageError) && hasLoadedAssets;
  const unavailableAssets = assets.filter(
    (asset) => asset.available === false,
  ).length;
  const listedDeepLinkedAsset = useMemo(
    () =>
      deepLinkedAssetId
        ? assets.find((asset) => asset.asset_id === deepLinkedAssetId)
        : null,
    [assets, deepLinkedAssetId],
  );
  const {
    data: deepLinkedAsset,
    isError: deepLinkedAssetError,
    isFetching: deepLinkedAssetFetching,
    refetch: refetchDeepLinkedAsset,
  } = useQuery({
    queryKey: ["library", "asset", deepLinkedAssetId, instanceView],
    enabled: Boolean(deepLinkedAssetId) && !listedDeepLinkedAsset,
    queryFn: () =>
      mediaRequest<{ asset: MediaAsset }>(
        `/me/api/media/${encodeURIComponent(deepLinkedAssetId)}${instanceView ? "?scope=everyone" : ""}`,
      ),
    retry: false,
  });
  useEffect(() => {
    if (!deepLinkedAssetId) {
      closedDeepLinkId.current = null;
      return;
    }
    if (closedDeepLinkId.current === deepLinkedAssetId) return;
    if (selected?.asset_id === deepLinkedAssetId) return;
    if (listedDeepLinkedAsset) {
      setSelected(listedDeepLinkedAsset);
      return;
    }
    if (deepLinkedAsset?.asset && isMediaKind(deepLinkedAsset.asset.kind)) {
      setSelected(deepLinkedAsset.asset);
      return;
    }
  }, [
    deepLinkedAsset,
    deepLinkedAssetId,
    listedDeepLinkedAsset,
    selected?.asset_id,
  ]);
  const models = useMemo(
    () =>
      Array.from(
        new Set([
          ...(mediaPages[0]?.models ?? []),
          ...assets.map((asset) => asset.model).filter(Boolean),
        ]),
      ).sort(),
    [assets, mediaPages],
  );
  const retryRetainedError = () => {
    if (isFetchNextPageError) {
      void fetchNextPage();
      return;
    }
    void refetch();
  };
  const deleteMutation = useMutation({
    mutationFn: async (asset: MediaAsset) => {
      const session = await mediaRequest<{ csrf_token: string }>(
        "/me/api/session",
      );
      return mediaRequest(
        `/me/api/media/${encodeURIComponent(asset.asset_id)}`,
        {
          method: "DELETE",
          headers: {
            "x-azizam-user-action": "1",
            "x-azizam-csrf": session.csrf_token,
          },
        },
      );
    },
    onSuccess: () => {
      toast.success("Deleted from your library.");
      closeAsset();
      void queryClient.invalidateQueries({ queryKey: ["library"] });
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Delete failed. Try again.",
      ),
  });
  return (
    <div>
      <PageHeader
        title="Library"
        subtitle={
          instanceView
            ? "Everything made through your gateway."
            : "Everything I’ve made."
        }
      />
      <section aria-label="Library filters" className="mb-6">
        <details className="group sm:hidden">
          <summary className="border-input bg-background hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring inline-flex min-h-11 cursor-pointer list-none items-center rounded-md border px-4 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none">
            Filter{activeFilters ? " applied" : ""}
          </summary>
          <div className="mt-4 space-y-4">
            <LibraryFilterControls
              kind={kind}
              setKind={setKind}
              search={search}
              setSearch={setSearch}
              model={model}
              setModel={setModel}
              models={models}
              date={date}
              setDate={setDate}
              owner={owner}
              setOwner={setOwner}
              source={source}
              setSource={setSource}
              sources={mediaPages[0]?.sources ?? []}
              instanceView={instanceView}
              canChooseOwner={["owner", "superadmin"].includes(viewer.role)}
              mobileLabels
            />
          </div>
        </details>
        <div className="hidden space-y-4 sm:block">
          <LibraryFilterControls
            kind={kind}
            setKind={setKind}
            search={search}
            setSearch={setSearch}
            model={model}
            setModel={setModel}
            models={models}
            date={date}
            setDate={setDate}
            owner={owner}
            setOwner={setOwner}
            source={source}
            setSource={setSource}
            sources={mediaPages[0]?.sources ?? []}
            instanceView={instanceView}
            canChooseOwner={["owner", "superadmin"].includes(viewer.role)}
          />
        </div>
      </section>
      {activeFilters ? (
        <div className="mb-4">
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            Clear filters
          </Button>
        </div>
      ) : null}
      {deepLinkedAssetError ? (
        <div className="mb-4">
          <ErrorState
            message="That library item could not be loaded. Try again."
            onRetry={() => void refetchDeepLinkedAsset()}
            retrying={deepLinkedAssetFetching}
          />
        </div>
      ) : null}
      {unavailableAssets > 0 ? (
        <div
          className="mb-4 flex flex-wrap items-center gap-3 text-sm"
          role="status"
        >
          <span>Some media items are unavailable.</span>
          <Button variant="outline" size="sm" onClick={() => void refetch()}>
            Refresh library
          </Button>
        </div>
      ) : null}
      {isLoading ? (
        <div
          role="status"
          aria-label="Loading library"
          aria-busy="true"
          className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
        >
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-72 rounded-xl" />
          ))}
        </div>
      ) : null}
      {showInitialError ? (
        <ErrorState
          message="Your library could not be loaded. Try again."
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : null}
      {!isLoading && !showInitialError && assets.length === 0 ? (
        <EmptyState
          icon={CalendarDays}
          title={activeFilters ? "No matching media" : "No media yet"}
          description={
            activeFilters
              ? "No media matches these filters. Clear filters or try another kind, date, model or prompt."
              : "Images, videos and audio you generate will appear here with the prompt, model, workflow and download links."
          }
          action={
            activeFilters ? (
              <Button variant="outline" onClick={clearFilters}>
                Clear filters
              </Button>
            ) : (
              <Button asChild variant="outline">
                <a href="/ui/try/">Make something</a>
              </Button>
            )
          }
        />
      ) : null}
      {!isLoading && !showInitialError && assets.length > 0 ? (
        <div
          aria-busy={updating}
          className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
        >
          {assets.map((asset) => (
            <AssetCard
              key={asset.asset_id}
              asset={asset}
              onOpen={openAsset}
              everyone={instanceView}
              modelCatalog={modelCatalog}
            />
          ))}
        </div>
      ) : null}
      {updating ? (
        <p role="status" className="text-muted-foreground mt-3 text-sm">
          Updating
        </p>
      ) : null}
      {showRetainedError ? (
        <div
          role="alert"
          className="border-destructive/30 bg-destructive/5 text-destructive mt-6 flex flex-wrap items-center justify-center gap-3 rounded-lg border p-3 text-sm"
        >
          <span>
            {isFetchNextPageError
              ? "More library items could not be loaded."
              : "Library results could not be refreshed."}
          </span>
          <Button
            variant="outline"
            disabled={isFetchingNextPage}
            onClick={retryRetainedError}
          >
            {isFetchingNextPage
              ? "Loading more…"
              : isFetchNextPageError
                ? "Load more / Retry"
                : "Retry"}
          </Button>
        </div>
      ) : null}
      {hasNextPage && !showInitialError && !showRetainedError ? (
        <div className="mt-6 flex justify-center">
          <Button
            variant="outline"
            disabled={isFetchingNextPage}
            onClick={() => void fetchNextPage()}
          >
            {isFetchingNextPage ? "Loading more…" : "Load more"}
          </Button>
        </div>
      ) : null}
      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => !open && closeAsset()}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          {selected ? (
            <>
              <SheetHeader>
                <SheetTitle className="min-w-0 break-words">
                  {assetTitle(selected)}
                </SheetTitle>
                <SheetDescription>
                  {kindLabel(selected.kind)}
                  {selected.model &&
                  modelDisplayName(selected.model, modelCatalog)
                    ? ` · ${modelDisplayName(selected.model, modelCatalog)}`
                    : ""}
                </SheetDescription>
              </SheetHeader>
              <div className="space-y-5 px-4 pb-4">
                <div className="bg-muted overflow-hidden rounded-lg border">
                  <Preview
                    key={selected.download_url}
                    asset={selected}
                    large
                    everyone={instanceView}
                  />
                </div>
                {selected.prompt ? (
                  <section>
                    <h2 className="mb-2 text-sm font-medium">Prompt</h2>
                    <p className="bg-muted/30 min-w-0 rounded-lg border p-3 text-sm break-words">
                      {selected.prompt}
                    </p>
                  </section>
                ) : null}
                <div className="flex flex-wrap gap-2">
                  {selected.available !== false && (
                    <Button
                      asChild
                      disabled={downloadingAssetId === selected.asset_id}
                    >
                      <a
                        href={
                          selected.download_url ??
                          `/me/api/media/${encodeURIComponent(selected.asset_id)}/download`
                        }
                        download
                        onClick={(event) => {
                          event.preventDefault();
                          setDownloadingAssetId(selected.asset_id);
                          void prepareDownload(selected, instanceView).then(
                            () => {
                              toast.success("Download ready.");
                              setDownloadingAssetId(null);
                            },
                            (error) => {
                              toast.error(
                                error instanceof Error
                                  ? error.message
                                  : "Download could not be prepared. Try again.",
                              );
                              setDownloadingAssetId(null);
                            },
                          );
                        }}
                      >
                        <Download />
                        {downloadingAssetId === selected.asset_id
                          ? "Preparing…"
                          : "Download"}
                      </a>
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    disabled={!selected.prompt}
                    onClick={() => {
                      void navigator.clipboard
                        .writeText(selected.prompt || "")
                        .then(
                          () => toast.success("Prompt copied."),
                          () =>
                            toast.error(
                              "Prompt could not be copied. Select the text to copy it.",
                            ),
                        );
                    }}
                  >
                    <Copy />
                    Copy prompt
                  </Button>
                  {selected.request_id ? (
                    <Button asChild variant="outline">
                      <a
                        href={`/r/${encodeURIComponent(selected.request_id)}`}
                        aria-label={`Open receipt ${selected.request_id}`}
                      >
                        <ExternalLink /> View receipt
                      </a>
                    </Button>
                  ) : null}
                  {selected.workflow_download_url ? (
                    <Button asChild variant="outline">
                      <a href={selected.workflow_download_url} download>
                        <ExternalLink /> Download ComfyUI workflow
                      </a>
                    </Button>
                  ) : null}
                  {!instanceView ? (
                    <OverflowMenu label={`Actions for ${assetTitle(selected)}`}>
                      <OverflowMenuSeparator />
                      <OverflowMenuItem
                        danger
                        onSelect={() => setConfirming(selected)}
                      >
                        <Trash2 className="size-4" aria-hidden />
                        Delete
                      </OverflowMenuItem>
                    </OverflowMenu>
                  ) : null}
                </div>
                <details className="rounded-lg border p-4">
                  <summary className="focus-visible:outline-ring min-h-11 cursor-pointer py-3 font-medium focus-visible:outline-2">
                    Details
                  </summary>
                  <div className="mt-3 space-y-5">
                    <div className="grid gap-3 text-sm sm:grid-cols-2">
                      <Detail
                        label="Model"
                        value={
                          selected.model
                            ? modelDisplayName(selected.model, modelCatalog)
                            : ""
                        }
                      />
                      <Detail
                        label="Created"
                        value={
                          <RelativeTime ts={selected.created_at} fallback="" />
                        }
                        title={absolute(selected.created_at)}
                      />
                      <Detail label="Seed" value={selected.seed ?? ""} />
                      <Detail
                        label="Size"
                        value={
                          selected.width && selected.height
                            ? `${selected.width} × ${selected.height} px`
                            : ""
                        }
                      />
                      <Detail
                        label="Generation time"
                        value={
                          selected.generation_ms == null
                            ? ""
                            : `${(selected.generation_ms / 1000).toFixed(1)} s`
                        }
                      />
                      <Detail
                        label="Request receipt"
                        value={
                          selected.request_id ? (
                            <a
                              className="text-primary text-xs break-all underline-offset-4 hover:underline"
                              href={`/r/${encodeURIComponent(selected.request_id)}`}
                              aria-label={`View receipt ${selected.request_id}`}
                            >
                              {selected.request_id}
                            </a>
                          ) : (
                            "Not recorded"
                          )
                        }
                        title={selected.request_id ?? undefined}
                      />
                      {selected.owner_name ? (
                        <Detail label="Owner" value={selected.owner_name} />
                      ) : null}
                      {selected.source ? (
                        <Detail label="Source" value={selected.source.label} />
                      ) : null}
                    </div>
                    <section>
                      <h2 className="mb-2 text-sm font-medium">Workflow</h2>
                      <pre className="bg-muted/30 max-h-40 overflow-auto rounded-lg border p-3 text-xs whitespace-pre-wrap">
                        {selected.workflow
                          ? typeof selected.workflow === "string"
                            ? selected.workflow
                            : JSON.stringify(selected.workflow, null, 2)
                          : "Not recorded"}
                      </pre>
                    </section>
                  </div>
                </details>
              </div>
            </>
          ) : null}
        </SheetContent>
      </Sheet>
      <ConfirmDialog
        open={Boolean(confirming)}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={`Delete ${confirming ? assetTitle(confirming) : "this media item"}?`}
        description="This removes the item from your library. Existing downloaded copies are not affected."
        confirmLabel="Delete"
        onConfirm={async () => {
          if (confirming) await deleteMutation.mutateAsync(confirming);
        }}
      />
    </div>
  );
}

function Detail({
  label,
  value,
  title,
}: {
  label: string;
  value: React.ReactNode;
  title?: string;
}) {
  if (value == null || value === "") return null;
  return (
    <dl className="min-w-0 rounded-lg border p-3">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="mt-1 truncate text-sm font-medium" title={title}>
        {value}
      </dd>
    </dl>
  );
}
