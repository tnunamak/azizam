"use client";

import { useMemo, useRef, useState } from "react";
import NextImage from "next/image";
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
  if (prompt) return prompt;
  return `${kindLabel(asset.kind)} generation`;
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
    return "Duration not measured yet";
  }
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

function absolute(ts: MediaAsset["created_at"]) {
  return ts ? formatAbsoluteUTC(ts) : undefined;
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

  const label = isPlaying ? `Pause ${title}` : `Play ${title}`;
  const durationLabel =
    error === "media"
      ? "Audio unavailable"
      : error === "playback"
        ? "Playback blocked. Try again."
        : formatDuration(measuredDuration);

  return (
    <div
      className="bg-background/95 absolute right-3 bottom-3 left-3 z-20 flex min-h-12 items-center gap-3 rounded-lg border p-2 shadow-sm backdrop-blur"
      onClick={(event) => event.stopPropagation()}
    >
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        onLoadedMetadata={(event) => {
          setError(null);
          setMeasuredDuration(event.currentTarget.duration);
        }}
        onPlay={() => {
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
          if (error === "media") {
            audio.load();
          }
          try {
            await audio.play();
            setError(null);
            setIsPlaying(true);
          } catch {
            setIsPlaying(false);
            setError("playback");
          }
        }}
      >
        {isPlaying ? <Pause aria-hidden /> : <Play aria-hidden />}
      </Button>
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium">Audio preview</p>
        <p
          className={cn(
            "text-muted-foreground text-xs",
            error === "media" && "text-destructive",
          )}
        >
          {durationLabel}
        </p>
      </div>
    </div>
  );
}

function Preview({
  asset,
  large = false,
  everyone = false,
}: {
  asset: MediaAsset;
  large?: boolean;
  everyone?: boolean;
}) {
  const src = asset.download_url ?? null;
  const Icon = kindIcon(asset.kind);
  const { data: poster } = useQuery({
    queryKey: ["library", "poster", asset.poster_asset_id, everyone],
    enabled: asset.kind === "video" && !!asset.poster_asset_id,
    queryFn: () =>
      mediaRequest<{ download_url: string }>(
        `/me/api/media/${encodeURIComponent(asset.poster_asset_id!)}/download?scope=${everyone ? "everyone" : "me"}`,
      ),
    retry: false,
  });
  if (asset.available === false)
    return (
      <div className="bg-muted text-muted-foreground flex h-full min-h-32 items-center justify-center p-4 text-center text-sm">
        {asset.unavailable_reason || "The media file is missing or expired."}
      </div>
    );
  if (asset.kind === "image" && src)
    return (
      <NextImage
        unoptimized
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={src}
        alt={assetTitle(asset)}
        className={cn(
          "h-full w-full",
          large ? "rounded-lg object-contain" : "object-cover",
        )}
      />
    );
  if (asset.kind === "video" && src && large)
    return (
      <video
        src={src}
        poster={poster?.download_url}
        preload="metadata"
        className={cn(
          "h-full w-full",
          large ? "rounded-lg object-contain" : "object-cover",
        )}
        controls
      />
    );
  if (asset.kind === "video" && poster?.download_url)
    return (
      <NextImage
        unoptimized
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={poster.download_url}
        alt={assetTitle(asset)}
        className="h-full w-full object-cover"
      />
    );
  if (asset.kind === "audio")
    return large && src ? (
      <audio
        controls
        src={src}
        aria-label="Generated audio"
        className="w-full"
      />
    ) : (
      <div className="bg-capability-voice/10 text-capability-voice flex h-full flex-col items-center justify-center gap-3 pb-12">
        <AudioLines className="size-12" aria-hidden />
        {!src && (
          <p className="text-foreground text-sm">Audio preview unavailable</p>
        )}
      </div>
    );
  return (
    <div
      className={cn(
        "flex h-full items-center justify-center",
        asset.kind === "video"
          ? "bg-capability-video/10 text-capability-video"
          : "bg-capability-image/10 text-capability-image",
      )}
    >
      <Icon className="size-8" aria-hidden />
    </div>
  );
}

function AssetCard({
  asset,
  onOpen,
  everyone,
}: {
  asset: MediaAsset;
  onOpen: (asset: MediaAsset) => void;
  everyone: boolean;
}) {
  const Icon = kindIcon(asset.kind);
  return (
    <article className="group bg-card hover:border-primary/50 relative min-w-0 overflow-hidden rounded-xl border transition">
      <button
        type="button"
        onClick={() => onOpen(asset)}
        className="focus-visible:ring-ring absolute inset-0 z-10 rounded-xl focus-visible:ring-2 focus-visible:outline-none focus-visible:ring-inset"
        aria-label={`Open ${assetTitle(asset)}`}
      />
      <div className="bg-muted relative aspect-[4/3]">
        <Preview asset={asset} everyone={everyone} />
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
      <div className="space-y-2 p-3">
        <p className="line-clamp-2 min-h-10 text-sm font-medium">
          {assetTitle(asset)}
        </p>
        <div className="text-muted-foreground flex items-center justify-between gap-2 text-xs">
          <span className="truncate">{asset.model || "Unknown model"}</span>
          <RelativeTime ts={asset.created_at} fallback="Time not recorded" />
        </div>
        {asset.source && (
          <p
            className="text-muted-foreground truncate text-xs"
            title={asset.source.label}
          >
            {asset.source.label}
          </p>
        )}
      </div>
    </article>
  );
}

export default function LibraryPage() {
  const viewer = useViewer();
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("all");
  const [model, setModel] = useState("all");
  const [date, setDate] = useState<DateFilter>("all");
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("default");
  const [source, setSource] = useState("all");
  const [selected, setSelected] = useState<MediaAsset | null>(null);
  const [confirming, setConfirming] = useState<MediaAsset | null>(null);
  const instanceView =
    ["owner", "superadmin"].includes(viewer.role) && owner !== "mine";
  const filters = { kind, model, date, search, owner, source };
  const mediaPath = buildMediaPath(filters);
  const {
    data,
    isLoading,
    isError,
    isFetching,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
    refetch,
  } = useInfiniteQuery({
    queryKey: ["library", filters],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      mediaRequest<MediaResponse>(
        mediaPath +
          (pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  const assets = useMemo(
    () =>
      (data?.pages.flatMap((page) => page.assets) ?? []).filter((item) =>
        isMediaKind(item.kind),
      ),
    [data?.pages],
  );
  const models = useMemo(
    () =>
      Array.from(
        new Set([
          ...(data?.pages[0]?.models ?? []),
          ...assets.map((asset) => asset.model).filter(Boolean),
        ]),
      ).sort(),
    [assets, data?.pages],
  );
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
            "x-ai-gateway-user-action": "1",
            "x-ai-gateway-csrf": session.csrf_token,
          },
        },
      );
    },
    onSuccess: () => {
      toast.success("Deleted from your library.");
      setSelected(null);
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
        answer={
          instanceView
            ? "Everything made through your gateway."
            : "Everything I’ve made."
        }
        description="Browse your images, video and audio with provenance and download actions."
      />
      <section aria-label="Library filters" className="mb-6 space-y-4">
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
          {["owner", "superadmin"].includes(viewer.role) ? (
            <Select
              value={owner === "default" ? "everyone" : owner}
              onValueChange={(value) => {
                setOwner(value);
                setSource("all");
              }}
            >
              <SelectTrigger aria-label="Owner" className="w-full sm:w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="mine">Mine</SelectItem>
                <SelectItem value="everyone">Everyone’s</SelectItem>
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
                {(data?.pages[0]?.sources ?? []).map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
        </div>
      </section>
      {instanceView ? (
        <p className="border-warning/30 bg-warning/10 text-muted-foreground mb-4 rounded-lg border px-3 py-2 text-sm">
          Showing media from all people, service apps and the local network.
          Access through this owner view is audited.
        </p>
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
      {isError ? (
        <ErrorState
          message="Your library could not be loaded. Try again."
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : null}
      {!isLoading && !isError && assets.length === 0 ? (
        <EmptyState
          icon={CalendarDays}
          title="No media yet"
          description={
            kind !== "all" || model !== "all" || date !== "all" || search
              ? "No media matches these filters. Try another kind, date or prompt."
              : "Images, videos and audio you generate will appear here with the prompt, model, workflow and download links."
          }
          action={
            <Button asChild variant="outline">
              <a href="/ui/try/">Make something</a>
            </Button>
          }
        />
      ) : null}
      {!isLoading && !isError && assets.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {assets.map((asset) => (
            <AssetCard
              key={asset.asset_id}
              asset={asset}
              onOpen={setSelected}
              everyone={instanceView}
            />
          ))}
        </div>
      ) : null}
      {hasNextPage && !isError ? (
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
        onOpenChange={(open) => !open && setSelected(null)}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          {selected ? (
            <>
              <SheetHeader>
                <SheetTitle>{assetTitle(selected)}</SheetTitle>
                <SheetDescription>
                  {kindLabel(selected.kind)} made with{" "}
                  {selected.model || "an unknown model"}
                </SheetDescription>
              </SheetHeader>
              <div className="space-y-5 px-4 pb-4">
                <div className="bg-muted overflow-hidden rounded-lg border">
                  <Preview asset={selected} large everyone={instanceView} />
                </div>
                <div className="grid gap-3 text-sm sm:grid-cols-2">
                  <Detail
                    label="Model"
                    value={selected.model || "Not measured yet"}
                  />
                  <Detail
                    label="Created"
                    value={
                      <RelativeTime
                        ts={selected.created_at}
                        fallback="Time not recorded"
                      />
                    }
                    title={absolute(selected.created_at)}
                  />
                  <Detail
                    label="Seed"
                    value={selected.seed ?? "Not measured yet"}
                  />
                  <Detail
                    label="Size"
                    value={
                      selected.width && selected.height
                        ? `${selected.width} × ${selected.height} px`
                        : "Not measured yet"
                    }
                  />
                  <Detail
                    label="Generation time"
                    value={
                      selected.generation_ms == null
                        ? "Not measured yet"
                        : `${(selected.generation_ms / 1000).toFixed(1)} s`
                    }
                  />
                  <Detail
                    label="Request receipt"
                    value={
                      selected.request_id ? (
                        <a
                          href={`/r/${encodeURIComponent(selected.request_id)}`}
                          aria-label={`View receipt ${selected.request_id}`}
                          className="text-primary focus-visible:ring-ring inline-flex min-h-11 max-w-full min-w-0 items-center gap-1 rounded-md underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:outline-none"
                        >
                          <span className="shrink-0">View receipt</span>
                          <span className="text-muted-foreground truncate font-mono text-xs">
                            {selected.request_id}
                          </span>
                          <ExternalLink
                            className="size-3 shrink-0"
                            aria-hidden
                          />
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
                  <h2 className="mb-2 text-sm font-medium">Prompt</h2>
                  <p className="bg-muted/30 rounded-lg border p-3 text-sm">
                    {selected.prompt ||
                      "Not recorded. The gateway does not report whether the prompt was omitted by policy or unavailable from the backend."}
                  </p>
                </section>
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
                <div className="flex flex-wrap gap-2">
                  {selected.available !== false && (
                    <Button asChild variant="outline">
                      <a
                        href={
                          selected.download_url ??
                          `/me/api/media/${encodeURIComponent(selected.asset_id)}/download`
                        }
                        download
                      >
                        <Download />
                        Download
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
                  {selected.workflow_download_url ? (
                    <Button asChild variant="outline">
                      <a href={selected.workflow_download_url} download>
                        <ExternalLink /> Open in ComfyUI
                      </a>
                    </Button>
                  ) : null}
                  {!instanceView ? (
                    <Button
                      variant="outline"
                      onClick={() => setConfirming(selected)}
                    >
                      <Trash2 />
                      Delete
                    </Button>
                  ) : null}
                </div>
              </div>
            </>
          ) : null}
        </SheetContent>
      </Sheet>
      <ConfirmDialog
        open={Boolean(confirming)}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Delete this media item?"
        description="This removes it from your library. Existing downloaded copies are not affected."
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
  return (
    <dl className="min-w-0 rounded-lg border p-3">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="mt-1 truncate text-sm font-medium" title={title}>
        {value}
      </dd>
    </dl>
  );
}
