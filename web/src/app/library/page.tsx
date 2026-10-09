"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import NextImage from "next/image";
import { useSearchParams } from "next/navigation";
import {
  useMutation,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Dialog as DialogPrimitive } from "radix-ui";
import {
  AudioLines,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  ImageIcon,
  ListFilter,
  Pause,
  Play,
  ReceiptText,
  Search,
  Trash2,
  Video,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/cn";
import { formatAbsoluteUTC } from "@/lib/format";
import { RelativeTime } from "@/components/product/relative-time";
import { ErrorState } from "@/components/product/error-state";
import { EmptyState } from "@/components/product/empty-state";
import { PersonMark } from "@/components/product/person-mark";
import {
  AudioCover,
  audioKind,
  CoverWash,
  coverSurface,
  Waveform,
} from "@/components/product/audio-cover";
import { useAudioPeaks } from "@/lib/audio-peaks";
import { Segmented } from "@/components/product/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
} from "@/components/ui/overflow-menu";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useViewer } from "@/components/shell/session";
import { productApi, type MemberStatus } from "@/lib/product-api";
import { api } from "@/lib/api";
import {
  MAKING_COPY,
  inProgressMedia,
  modelDisplayName,
  tileDate,
  captionTitle,
  type CapabilityNameRow,
  type InProgressItem,
} from "./library-format";

type NameModel = (model: string) => string;

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
  source?: { id?: string; kind: string; label: string; app_id?: string };
  available?: boolean;
  unavailable_reason?: string | null;
}

interface MediaResponse {
  assets: MediaAsset[];
  next_cursor?: string | null;
  models?: string[];
  sources?: MediaSource[];
}

interface MediaSource {
  id: string;
  label: string;
  kind?: string;
  app_id?: string;
  owner_user_id?: string | null;
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

/** A heading-length title: the prompt's first clause, or its first words. */
function shortTitle(asset: MediaAsset): string {
  const prompt = typeof asset.prompt === "string" ? asset.prompt.trim() : "";
  if (!prompt) return kindLabel(asset.kind);
  const clause =
    prompt.split(/(?<=[.!?])\s|,\s/)[0]?.replace(/[.!?]$/, "") ?? prompt;
  const words = clause.split(/\s+/);
  return words.length <= 12 ? clause : `${words.slice(0, 10).join(" ")}…`;
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
  else if (filters.owner === "mine") params.set("scope", "me");
  else if (filters.owner !== "default") params.set("person", filters.owner);
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

/** Only a confirmed missing or expired file is "no longer stored". */
function confirmedGone(reason?: string | null): boolean {
  return /missing or expired/i.test(reason?.replace(/[_-]+/g, " ") ?? "");
}

/**
 * Plain words for a tile that shows no media; the raw reason stays in Details.
 * A file the gateway reports unavailable is "unavailable"; a preview that only
 * failed to load says no more than that.
 */
function unavailableCopy(asset: MediaAsset): string {
  if (confirmedGone(asset.unavailable_reason)) return "File no longer stored";
  if (asset.available === false) return "This file is unavailable.";
  return "Preview unavailable";
}

function UnavailablePreview({
  kind,
  copy,
  onRetry,
}: {
  kind: MediaKind;
  copy: string;
  onRetry?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  const Icon = kindIcon(kind);
  return (
    <div className="bg-muted text-muted-foreground flex h-full min-h-32 flex-col items-center justify-center gap-2 p-3 text-center">
      <div role="img" aria-label={`${kindLabel(kind)} preview unavailable`}>
        <Icon aria-hidden="true" className="mx-auto size-5" />
      </div>
      <p className="text-xs">{copy}</p>
      {onRetry ? (
        <Button
          type="button"
          variant="link"
          size="xs"
          className="relative z-20 max-sm:min-h-11"
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

function pauseOtherAudio(current: HTMLAudioElement) {
  current.ownerDocument.querySelectorAll("audio").forEach((audio) => {
    if (audio !== current) audio.pause();
  });
}

/** The one duration chip, shared by video and audio tiles. */
function DurationChip({
  icon: Icon,
  children,
  className,
}: {
  icon: typeof Play;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "bg-chrome/80 text-chrome-foreground pointer-events-none flex h-6 shrink-0 items-center gap-1 rounded-full px-2 text-xs font-medium",
        className,
      )}
    >
      <Icon aria-hidden="true" className="size-3" />
      {children}
    </span>
  );
}

/**
 * An audio creation has no picture, so its tile is an AudioCover: the maker's
 * tint, the kind of sound, its words, its real waveform and its length, with
 * one play control.
 */
function AudioTile({
  assetId,
  src,
  title,
  text,
  duration,
  visible,
  maker,
  meta,
  model,
}: {
  assetId: string;
  src: string;
  title: string;
  text: string;
  duration?: number | null;
  visible: boolean;
  maker?: string | null;
  /** Who and when, on the cover's top line ("Robin · Monday"). */
  meta?: string;
  model?: string | null;
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
  const errorText =
    error === "media"
      ? "Audio unavailable"
      : error === "playback"
        ? "Playback didn't start. Try again."
        : null;
  const durationText =
    measuredDuration != null &&
    Number.isFinite(measuredDuration) &&
    measuredDuration >= 0
      ? formatDuration(measuredDuration)
      : null;

  return (
    <AudioCover
      peaksKey={assetId}
      src={src}
      visible={visible}
      maker={maker}
      meta={meta}
      miniOnPhone
      model={model}
      text={text}
      duration={durationText}
      status={errorText ? <span role="status">{errorText}</span> : null}
      control={
        <>
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
            variant="cover"
            className="relative z-20 size-9 shrink-0 rounded-full max-sm:size-11"
            aria-label={label}
            onClick={async (event) => {
              event.stopPropagation();
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
        </>
      }
    />
  );
}

/** The viewer's player: one large play control, a seek bar and both times. */
function LargeAudioPlayer({
  src,
  title,
  duration,
  peaksKey,
  onFailed,
}: {
  src: string;
  title: string;
  duration?: number | null;
  peaksKey: string;
  onFailed: () => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState<number | null>(duration ?? null);
  const [blocked, setBlocked] = useState(false);
  const peaks = useAudioPeaks(peaksKey, src, true, 64);
  return (
    <div
      data-media-controls
      className="relative flex w-full flex-col items-center gap-5"
    >
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        aria-label="Generated audio"
        onError={onFailed}
        onPlay={(event) => {
          pauseOtherAudio(event.currentTarget);
          setBlocked(false);
          setPlaying(true);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => {
          if (Number.isFinite(event.currentTarget.duration))
            setLength(event.currentTarget.duration);
        }}
        className="sr-only"
      />
      <Button
        type="button"
        size="icon"
        variant="cover"
        className="size-16 rounded-full [&_svg:not([class*='size-'])]:size-6"
        aria-label={`${playing ? "Pause" : "Play"} ${title}`}
        onClick={async () => {
          const audio = audioRef.current;
          if (!audio) return;
          if (playing) return audio.pause();
          try {
            await audio.play();
          } catch {
            setBlocked(true);
          }
        }}
      >
        {playing ? <Pause aria-hidden /> : <Play aria-hidden />}
      </Button>
      {peaks ? (
        <Waveform
          peaks={peaks}
          progress={length ? Math.min(1, time / length) : 0}
          className="h-14 w-full flex-none text-(--person)"
        />
      ) : null}
      <div className="flex w-full items-center gap-3">
        <span className="figures text-muted-foreground w-10 text-right text-xs">
          {formatDuration(time)}
        </span>
        <input
          type="range"
          min={0}
          max={length ?? 0}
          step={0.1}
          value={Math.min(time, length ?? 0)}
          disabled={!length}
          aria-label="Position"
          onChange={(event) => {
            const audio = audioRef.current;
            if (!audio) return;
            audio.currentTime = Number(event.target.value);
            setTime(audio.currentTime);
          }}
          className="h-11 min-w-0 flex-1 cursor-pointer accent-(--person)"
        />
        <span className="figures text-muted-foreground w-10 text-xs">
          {length != null ? formatDuration(length) : "–:––"}
        </span>
      </div>
      {blocked ? (
        <p role="status" className="text-sm">
          Playback didn&apos;t start. Try again.
        </p>
      ) : null}
    </div>
  );
}

/**
 * The viewer's video player, built like LargeAudioPlayer: one play control, a
 * seek bar with both times, and a mute toggle. The video itself is the
 * focusable stage: Space plays or pauses, the arrow keys seek.
 */
function LargeVideoPlayer({
  src,
  poster,
  title,
  onFailed,
}: {
  src: string;
  poster?: string;
  title: string;
  onFailed: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState<number | null>(null);
  const [muted, setMuted] = useState(false);
  const [blocked, setBlocked] = useState(false);
  async function toggle() {
    const video = videoRef.current;
    if (!video) return;
    if (!video.paused) return video.pause();
    try {
      await video.play();
    } catch {
      setBlocked(true);
    }
  }
  return (
    <div className="flex size-full min-h-0 flex-col">
      <video
        ref={videoRef}
        src={src}
        poster={poster}
        preload="auto"
        playsInline
        tabIndex={0}
        aria-label={title}
        onClick={() => void toggle()}
        onKeyDown={(event) => {
          const video = event.currentTarget;
          if (event.key === " ") {
            event.preventDefault();
            void toggle();
          } else if (
            (event.key === "ArrowLeft" || event.key === "ArrowRight") &&
            Number.isFinite(video.duration)
          ) {
            event.preventDefault();
            video.currentTime = Math.min(
              video.duration,
              Math.max(
                0,
                video.currentTime + (event.key === "ArrowLeft" ? -5 : 5),
              ),
            );
          }
        }}
        onError={onFailed}
        onPlay={(event) => {
          pauseOtherAudio(event.currentTarget);
          setBlocked(false);
          setPlaying(true);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onVolumeChange={(event) => setMuted(event.currentTarget.muted)}
        onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => {
          if (Number.isFinite(event.currentTarget.duration))
            setLength(event.currentTarget.duration);
        }}
        className="min-h-0 w-full flex-1 cursor-pointer object-contain"
      />
      <div
        data-media-controls
        className="text-chrome-foreground flex items-center gap-3 px-3 py-2"
      >
        <Button
          type="button"
          size="icon"
          className="size-11 rounded-full"
          aria-label={`${playing ? "Pause" : "Play"} ${title}`}
          onClick={() => void toggle()}
        >
          {playing ? <Pause aria-hidden /> : <Play aria-hidden />}
        </Button>
        <span className="figures w-10 text-right text-xs">
          {formatDuration(time)}
        </span>
        <input
          type="range"
          min={0}
          max={length ?? 0}
          step={0.1}
          value={Math.min(time, length ?? 0)}
          disabled={!length}
          aria-label="Position"
          onChange={(event) => {
            const video = videoRef.current;
            if (!video) return;
            video.currentTime = Number(event.target.value);
            setTime(video.currentTime);
          }}
          className="accent-primary h-11 min-w-0 flex-1 cursor-pointer"
        />
        <span className="figures w-10 text-xs">
          {length != null ? formatDuration(length) : "–:––"}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={muted ? "Unmute" : "Mute"}
          onClick={() => {
            const video = videoRef.current;
            if (!video) return;
            video.muted = !video.muted;
            setMuted(video.muted);
          }}
          className="bg-chrome/70 text-chrome-foreground hover:bg-chrome rounded-full"
        >
          {muted ? <VolumeX aria-hidden /> : <Volume2 aria-hidden />}
        </Button>
      </div>
      {blocked ? (
        <p role="status" className="text-chrome-foreground px-3 pb-2 text-sm">
          Playback didn&apos;t start. Try again.
        </p>
      ) : null}
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
        className="bg-muted h-full min-h-32 animate-pulse"
      />
    );
  if (unavailable)
    return (
      <UnavailablePreview
        kind={asset.kind}
        copy={unavailableCopy(asset)}
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
          large ? "object-contain" : "object-cover",
        )}
      />
    );
  if (
    asset.kind === "video" &&
    currentSrc &&
    (!asset.poster_asset_id || !waitingForPoster) &&
    (large || posterUnavailable)
  )
    return large ? (
      <LargeVideoPlayer
        key={`video-${asset.asset_id}-${retryKey}`}
        src={currentSrc}
        poster={effectivePosterUrl}
        title={assetTitle(asset)}
        onFailed={() => setMediaFailed(true)}
      />
    ) : (
      // No poster (the gateway records none for real video jobs): load
      // metadata only and show the frame at 0.1 s. The media fragment asks
      // Safari for that frame too, which ignores the seek below until played;
      // the server never sees a fragment. The stage colour keeps a blank frame
      // reading as media, not as a missing file.
      <video
        key={`video-${asset.asset_id}-${retryKey}`}
        src={visible ? `${currentSrc}#t=0.1` : undefined}
        preload="metadata"
        muted
        playsInline
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (Number.isFinite(video.duration) && video.duration > 0) {
            video.currentTime = Math.min(0.1, video.duration / 2);
          }
        }}
        onError={() => setMediaFailed(true)}
        className="bg-media-stage h-full w-full object-cover"
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
      <LargeAudioPlayer
        src={currentSrc}
        title={assetTitle(asset)}
        duration={asset.duration_s}
        peaksKey={asset.asset_id}
        onFailed={() => setMediaFailed(true)}
      />
    ) : (
      <div className="bg-media-stage text-chrome-foreground flex h-full items-center justify-center">
        <AudioLines className="size-8" aria-hidden />
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

/* ------------------------------------------------------------------ */
/* People: who made a creation                                         */
/* ------------------------------------------------------------------ */

type PersonLookup = (asset: MediaAsset) => string | null;

function usePeople(enabled: boolean, sources: MediaSource[]) {
  const { data: users } = useQuery({
    queryKey: ["library", "people"],
    queryFn: api.users.list,
    enabled,
    retry: false,
  });
  const people = useMemo(
    () =>
      (users ?? [])
        .map((user) => ({
          id: user.id,
          name: (user.display_name || user.name || "").trim(),
        }))
        .filter((person) => person.name && !person.name.includes("@")),
    [users],
  );
  const lookup: PersonLookup = (asset) => {
    if (asset.owner_name) return asset.owner_name;
    const appId =
      asset.source?.app_id ??
      (asset.source?.id?.startsWith("app:")
        ? asset.source.id.slice(4)
        : undefined);
    const owner = sources.find(
      (source) => source.app_id === appId,
    )?.owner_user_id;
    return people.find((person) => person.id === owner)?.name ?? null;
  };
  return { people, lookup };
}

/* ------------------------------------------------------------------ */
/* Grid                                                                */
/* ------------------------------------------------------------------ */

function toDateValue(ts: MediaAsset["created_at"]): Date | null {
  if (ts == null || ts === "") return null;
  const date =
    typeof ts === "number"
      ? new Date(ts < 5_000_000_000 ? ts * 1000 : ts)
      : new Date(ts);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Work the gateway accepted and is still making: calm, not clickable. */
function InProgressTile({ item }: { item: InProgressItem }) {
  const Icon = kindIcon(item.kind);
  return (
    <div className="bg-muted text-muted-foreground rounded-media flex aspect-square min-w-0 flex-col items-center justify-center gap-2 p-3 text-center">
      <Icon aria-hidden="true" className="size-5" />
      <p className="text-foreground text-sm font-medium max-sm:sr-only">
        {MAKING_COPY[item.kind]}
      </p>
      {item.startedAt != null ? (
        <p className="text-xs max-sm:sr-only">
          Started <RelativeTime ts={item.startedAt} fallback="" />
        </p>
      ) : null}
    </div>
  );
}

function AssetTile({
  asset,
  onOpen,
  everyone,
  person,
  selected,
}: {
  asset: MediaAsset;
  onOpen: (asset: MediaAsset) => void;
  everyone: boolean;
  person: string | null;
  selected: boolean;
}) {
  const { ref: visibilityRef, visible } = usePreviewVisible();
  const playableAudio =
    asset.kind === "audio" && asset.download_url && asset.available !== false;
  return (
    <article className="group relative min-w-0">
      <button
        type="button"
        data-asset-open={asset.asset_id}
        onClick={() => onOpen(asset)}
        className="rounded-media absolute inset-0 z-10"
        aria-label={`Open ${assetTitle(asset)}`}
      />
      <div
        ref={visibilityRef}
        className={cn(
          "rounded-media relative aspect-square overflow-hidden",
          selected && "outline-ring outline-2 outline-offset-2",
        )}
      >
        {playableAudio ? (
          <AudioTile
            key={asset.download_url}
            assetId={asset.asset_id}
            src={asset.download_url!}
            title={assetTitle(asset)}
            text={asset.prompt?.trim() || kindLabel(asset.kind)}
            duration={asset.duration_s}
            visible={visible}
            maker={person}
            meta={[
              everyone ? (person?.split(" ")[0] ?? null) : null,
              tileDate(toDateValue(asset.created_at), new Date()),
            ]
              .filter(Boolean)
              .join(" · ")}
            model={asset.model}
          />
        ) : (
          <Preview
            key={`preview-${asset.asset_id}-${asset.download_url}`}
            asset={asset}
            everyone={everyone}
            visible={visible}
          />
        )}
        {asset.kind === "video" && asset.available !== false ? (
          <DurationChip icon={Play} className="absolute right-2 bottom-2 z-20">
            {asset.duration_s != null && Number.isFinite(asset.duration_s)
              ? formatDuration(asset.duration_s)
              : "Video"}
          </DurationChip>
        ) : null}
        {/* D1.4: always-on captions were cut to two or three words and the
            maker badge sat on every tile. The grid now shows media only; on
            a wide screen, hover or keyboard focus shows a short title (whole
            words, two lines), the maker and the day. The button's name
            carries the full prompt for screen readers. */}
        {asset.kind !== "audio" ? (
          <div
            aria-hidden="true"
            className="dark bg-card/90 text-foreground pointer-events-none absolute inset-x-0 bottom-0 z-20 px-2.5 py-2 opacity-0 transition-opacity duration-(--motion-fast) group-focus-within:opacity-100 group-hover:opacity-100 max-sm:hidden"
          >
            <p className="line-clamp-2 text-xs leading-snug font-medium">
              {captionTitle(asset.prompt, kindLabel(asset.kind), 56)}
            </p>
            <p className="text-foreground/80 mt-0.5 text-xs">
              {[
                everyone ? person : null,
                tileDate(toDateValue(asset.created_at), new Date()),
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </div>
        ) : null}
      </div>
    </article>
  );
}

/* ------------------------------------------------------------------ */
/* Filters                                                             */
/* ------------------------------------------------------------------ */

function MoreFilters({
  model,
  setModel,
  models,
  date,
  setDate,
  source,
  setSource,
  sources,
  instanceView,
  nameModel,
  phoneExtras,
  phoneActive = 0,
  onClear,
}: {
  model: string;
  setModel: (value: string) => void;
  models: string[];
  date: DateFilter;
  setDate: (value: DateFilter) => void;
  source: string;
  setSource: (value: string) => void;
  sources: MediaSource[];
  instanceView: boolean;
  nameModel: NameModel;
  /** Controls that live in the row on wide screens and here on a phone. */
  phoneExtras?: ReactNode;
  /** How many of those are set, for the count on a phone. */
  phoneActive?: number;
  /** Clear every filter; shown here on a phone, beside the row elsewhere. */
  onClear?: () => void;
}) {
  const active =
    (model !== "all" ? 1 : 0) +
    (date !== "all" ? 1 : 0) +
    (source !== "all" ? 1 : 0);
  const badge = (count: number, className: string) =>
    count ? (
      <span
        className={cn(
          "bg-primary text-primary-foreground figures size-5 items-center justify-center rounded-full text-xs",
          className,
        )}
      >
        {count}
        <span className="sr-only"> active</span>
      </span>
    ) : null;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline">
          <ListFilter aria-hidden="true" />
          Filters
          {/* A phone counts kind and person too, which live in here. */}
          {badge(active + phoneActive, "flex sm:hidden")}
          {badge(active, "max-sm:hidden sm:flex")}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 space-y-4">
        {phoneExtras ? (
          <div className="space-y-2 sm:hidden">{phoneExtras}</div>
        ) : null}
        <div className="space-y-1.5">
          <p className="text-sm font-medium">Model</p>
          <Select value={model} onValueChange={setModel}>
            <SelectTrigger aria-label="Model" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All models</SelectItem>
              {models.map((m) => (
                <SelectItem key={m} value={m}>
                  {nameModel(m)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <p className="text-sm font-medium">Date</p>
          <Select
            value={date}
            onValueChange={(value) => setDate(value as DateFilter)}
          >
            <SelectTrigger aria-label="Date" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any time</SelectItem>
              <SelectItem value="7d">Last 7 days</SelectItem>
              <SelectItem value="30d">Last 30 days</SelectItem>
              <SelectItem value="90d">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {instanceView ? (
          <div className="space-y-1.5">
            <p className="text-sm font-medium">App</p>
            <Select value={source} onValueChange={setSource}>
              <SelectTrigger aria-label="Source" className="w-full">
                <SelectValue placeholder="All apps" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All apps</SelectItem>
                {sources.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}
        {onClear ? (
          <Button
            variant="outline"
            onClick={onClear}
            className="w-full sm:hidden"
          >
            Clear filters
          </Button>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}

/* ------------------------------------------------------------------ */
/* Selected creation                                                   */
/* ------------------------------------------------------------------ */

interface ReceiptCost {
  remote?: boolean | null;
  cost?: { cost_class?: string; usd?: string | number | null } | null;
}

function costText(receipt: ReceiptCost): string {
  const usd = Number(receipt.cost?.usd);
  if (receipt.cost && Number.isFinite(usd) && usd > 0)
    return usd < 0.01 ? "Less than USD 0.01" : `USD ${usd.toFixed(2)}`;
  if (receipt.remote === false && receipt.cost?.cost_class === "local")
    return "Local · no provider charge";
  return "Cost not recorded";
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  if (children == null || children === "") return null;
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-4 py-2">
      <dt className="text-muted-foreground shrink-0 text-sm">{label}</dt>
      <dd className="figures min-w-0 text-right text-sm break-words">
        {children}
      </dd>
    </div>
  );
}

function CreationViewer({
  asset,
  assets,
  onSelect,
  onClose,
  everyone,
  nameModel,
  person,
  downloading,
  onDownload,
  onDelete,
  onReturnFocus,
}: {
  asset: MediaAsset;
  assets: MediaAsset[];
  onSelect: (asset: MediaAsset) => void;
  onClose: () => void;
  everyone: boolean;
  nameModel: NameModel;
  person: string | null;
  downloading: boolean;
  onDownload: (asset: MediaAsset) => void;
  onDelete: ((asset: MediaAsset) => void) | null;
  /** Called with the creation shown last when the viewer closes. */
  onReturnFocus: (assetId: string) => void;
}) {
  const index = assets.findIndex((item) => item.asset_id === asset.asset_id);
  const previous = index > 0 ? assets[index - 1] : null;
  const next =
    index >= 0 && index < assets.length - 1 ? assets[index + 1] : null;
  const {
    data: receipt,
    isPending: receiptPending,
    isError: receiptFailed,
  } = useQuery({
    queryKey: ["library", "receipt", asset.request_id, everyone],
    enabled: Boolean(asset.request_id),
    queryFn: () =>
      mediaRequest<ReceiptCost>(
        `/me/api/receipts/${encodeURIComponent(asset.request_id!)}${everyone ? "?scope=everyone" : ""}`,
      ),
    retry: false,
  });
  const created = toDateValue(asset.created_at);
  const model = asset.model ? nameModel(asset.model) : "";
  const title = shortTitle(asset);
  const prompt = asset.prompt?.trim() ?? "";
  const charge = !asset.request_id
    ? null
    : receiptPending
      ? "Checking…"
      : receiptFailed || !receipt
        ? "Couldn’t load the receipt"
        : costText(receipt);
  // The note names only what this creation still has.
  const remains =
    prompt && asset.request_id
      ? "The prompt and receipt are below."
      : prompt
        ? "The prompt is below."
        : asset.request_id
          ? "The receipt is below."
          : "";
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="bg-overlay data-[state=open]:animate-in data-[state=open]:fade-in-0 fixed inset-0 z-50" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            onReturnFocus(asset.asset_id);
          }}
          onKeyDown={(event) => {
            const target = event.target;
            if (target instanceof HTMLMediaElement) return;
            if (
              target instanceof Element &&
              target.closest(
                "input, textarea, select, [role='slider'], [data-media-controls]",
              )
            )
              return;
            if (event.key === "ArrowLeft" && previous) onSelect(previous);
            if (event.key === "ArrowRight" && next) onSelect(next);
          }}
          className="bg-background shadow-overlay data-[state=open]:animate-in data-[state=open]:fade-in-0 md:rounded-panel fixed inset-0 z-50 flex flex-col overflow-y-auto md:inset-6 md:grid md:grid-cols-[minmax(0,1fr)_minmax(0,26rem)] md:overflow-hidden lg:inset-10"
        >
          <div className="bg-media-stage relative flex min-h-[50dvh] shrink-0 items-center justify-center md:min-h-0">
            <div
              className={cn(
                "flex size-full items-center justify-center",
                asset.kind === "audio" ? "p-6" : "p-0 md:p-8",
              )}
            >
              <div
                className={cn(
                  "relative flex max-h-full w-full items-center justify-center overflow-hidden",
                  asset.kind === "audio"
                    ? cn(
                        coverSurface(person),
                        "rounded-panel max-w-lg flex-col gap-6 px-8 py-8",
                      )
                    : "h-full",
                )}
              >
                {asset.kind === "audio" ? (
                  <>
                    {/* The clip's own cover: maker's hue, kind, its words. */}
                    <CoverWash />
                    <div className="relative w-full space-y-3">
                      <p className="text-foreground/80 flex items-center gap-1.5 text-xs font-medium">
                        <AudioLines
                          aria-hidden="true"
                          className="size-3.5 text-(--person)"
                        />
                        {audioKind(asset.model).label}
                      </p>
                      <p className="line-clamp-5 text-lg leading-snug font-medium break-words">
                        {asset.prompt?.trim() || assetTitle(asset)}
                      </p>
                    </div>
                  </>
                ) : null}
                <Preview
                  key={`${asset.asset_id}-${asset.download_url}`}
                  asset={asset}
                  large
                  everyone={everyone}
                />
              </div>
            </div>
            {previous || next ? (
              <div className="pointer-events-none absolute inset-x-3 top-1/2 hidden -translate-y-1/2 justify-between md:flex">
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Previous creation"
                  disabled={!previous}
                  onClick={() => previous && onSelect(previous)}
                  className="bg-chrome/70 text-chrome-foreground hover:bg-chrome pointer-events-auto rounded-full disabled:opacity-0"
                >
                  <ChevronLeft aria-hidden="true" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Next creation"
                  disabled={!next}
                  onClick={() => next && onSelect(next)}
                  className="bg-chrome/70 text-chrome-foreground hover:bg-chrome pointer-events-auto rounded-full disabled:opacity-0"
                >
                  <ChevronRight aria-hidden="true" />
                </Button>
              </div>
            ) : null}
            <DialogPrimitive.Close
              className="bg-chrome/70 text-chrome-foreground hover:bg-chrome absolute top-3 left-3 flex size-11 items-center justify-center rounded-full md:hidden"
              aria-label="Close"
            >
              <X aria-hidden="true" className="size-5" />
            </DialogPrimitive.Close>
          </div>
          <aside className="flex min-h-0 min-w-0 flex-col md:overflow-y-auto md:border-l">
            <div className="flex-1 space-y-5 p-5">
              <div className="flex items-start justify-between gap-3">
                <DialogPrimitive.Title className="min-w-0 text-xl leading-snug font-semibold text-balance break-words">
                  {title}
                </DialogPrimitive.Title>
                <div className="-mt-1.5 -mr-1.5 flex shrink-0 items-center gap-1">
                  {onDelete ? (
                    <OverflowMenu label={`Actions for ${title}`}>
                      <OverflowMenuSeparator />
                      <OverflowMenuItem danger onSelect={() => onDelete(asset)}>
                        <Trash2 className="size-4" aria-hidden />
                        Delete
                      </OverflowMenuItem>
                    </OverflowMenu>
                  ) : null}
                  <DialogPrimitive.Close
                    className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-control hidden size-9 items-center justify-center md:flex"
                    aria-label="Close"
                  >
                    <X aria-hidden="true" className="size-4" />
                  </DialogPrimitive.Close>
                </div>
              </div>
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                {person ? (
                  <>
                    <PersonMark name={person} size="sm" />
                    <span className="font-medium">{person}</span>
                  </>
                ) : (
                  <span className="font-medium">{kindLabel(asset.kind)}</span>
                )}
                {asset.created_at ? (
                  <span className="text-muted-foreground">
                    <RelativeTime ts={asset.created_at} fallback="" />
                  </span>
                ) : null}
              </p>
              {asset.available === false ? (
                <p className="bg-muted rounded-control p-3 text-sm">
                  {confirmedGone(asset.unavailable_reason)
                    ? "The gateway no longer stores this file."
                    : "This file is unavailable."}
                  {remains ? ` ${remains}` : ""}
                </p>
              ) : null}
              <div className="flex flex-wrap gap-2">
                {asset.available !== false ? (
                  <Button
                    type="button"
                    size="sm"
                    disabled={downloading}
                    aria-busy={downloading || undefined}
                    onClick={() => onDownload(asset)}
                  >
                    <Download aria-hidden="true" />
                    {downloading ? "Preparing…" : "Download"}
                  </Button>
                ) : null}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!asset.prompt}
                  onClick={() => {
                    void navigator.clipboard.writeText(asset.prompt || "").then(
                      () => toast.success("Prompt copied."),
                      () =>
                        toast.error(
                          "Prompt could not be copied. Select the text to copy it.",
                        ),
                    );
                  }}
                >
                  <Copy aria-hidden="true" />
                  Copy prompt
                </Button>
                {asset.request_id ? (
                  <Button asChild variant="outline" size="sm">
                    <a
                      href={`/r/${encodeURIComponent(asset.request_id)}`}
                      aria-label={`Open receipt ${asset.request_id}`}
                    >
                      <ReceiptText aria-hidden="true" />
                      Receipt
                    </a>
                  </Button>
                ) : null}
              </div>
              <dl className="divide-y border-y">
                <Fact label="Model">{model}</Fact>
                <Fact label="Created">
                  {created
                    ? created.toLocaleString(undefined, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      })
                    : ""}
                </Fact>
                <Fact label="Size">
                  {asset.width && asset.height
                    ? `${asset.width} × ${asset.height}`
                    : ""}
                </Fact>
                <Fact label="Length">
                  {asset.duration_s != null && Number.isFinite(asset.duration_s)
                    ? formatDuration(asset.duration_s)
                    : ""}
                </Fact>
                <Fact label="Made in">
                  {asset.generation_ms == null
                    ? ""
                    : `${(asset.generation_ms / 1000).toFixed(1)} s`}
                </Fact>
                {charge ? <Fact label="Charge">{charge}</Fact> : null}
              </dl>
              {prompt && prompt.replace(/[.!?]$/, "") !== title ? (
                <section>
                  <h3 className="text-muted-foreground mb-1 text-sm font-medium">
                    Prompt
                  </h3>
                  <p className="text-sm break-words">{prompt}</p>
                </section>
              ) : null}
              <details className="group">
                <summary className="text-muted-foreground hover:text-foreground flex min-h-11 cursor-pointer items-center text-sm font-medium">
                  Details
                </summary>
                <div className="mt-2 grid gap-x-4 gap-y-3 text-sm sm:grid-cols-2">
                  <Detail
                    label="Request receipt"
                    value={
                      asset.request_id ? (
                        <a
                          className="text-link font-mono text-xs break-all underline-offset-4 hover:underline"
                          href={`/r/${encodeURIComponent(asset.request_id)}`}
                          aria-label={`View receipt ${asset.request_id}`}
                        >
                          {asset.request_id}
                        </a>
                      ) : (
                        "Not recorded"
                      )
                    }
                    title={asset.request_id ?? undefined}
                  />
                  <Detail
                    label="Model name"
                    value={
                      asset.model ? (
                        <span className="font-mono text-xs">{asset.model}</span>
                      ) : (
                        ""
                      )
                    }
                    title={asset.model || undefined}
                  />
                  <Detail label="Seed" value={asset.seed ?? ""} />
                  {asset.source ? (
                    <Detail label="App" value={asset.source.label} />
                  ) : null}
                  <Detail
                    label="Created (UTC)"
                    value={absolute(asset.created_at) ?? ""}
                  />
                  {asset.available === false && asset.unavailable_reason ? (
                    <Detail label="File" value={asset.unavailable_reason} />
                  ) : null}
                </div>
                <section className="mt-3">
                  <h4 className="mb-1 text-sm font-medium">Workflow</h4>
                  <pre className="bg-muted rounded-control max-h-40 overflow-auto p-3 font-mono text-xs whitespace-pre-wrap">
                    {asset.workflow
                      ? typeof asset.workflow === "string"
                        ? asset.workflow
                        : JSON.stringify(asset.workflow, null, 2)
                      : "Not recorded"}
                  </pre>
                  {asset.workflow_download_url ? (
                    <Button
                      asChild
                      variant="outline"
                      size="sm"
                      className="mt-2"
                    >
                      <a href={asset.workflow_download_url} download>
                        <Download aria-hidden="true" /> Download ComfyUI
                        workflow
                      </a>
                    </Button>
                  ) : null}
                </section>
              </details>
            </div>
            {previous || next ? (
              <div className="flex items-center justify-between border-t px-3 py-2 md:hidden">
                <Button
                  variant="ghost"
                  disabled={!previous}
                  onClick={() => previous && onSelect(previous)}
                >
                  <ChevronLeft aria-hidden="true" /> Newer
                </Button>
                <Button
                  variant="ghost"
                  disabled={!next}
                  onClick={() => next && onSelect(next)}
                >
                  Older <ChevronRight aria-hidden="true" />
                </Button>
              </div>
            ) : null}
          </aside>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
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
    <dl className="min-w-0">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="mt-0.5 truncate text-sm" title={title}>
        {value}
      </dd>
    </dl>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

/**
 * Return focus to the tile of the creation shown last; if that tile is gone,
 * to the grid's first tile; else to the page heading.
 */
function focusTile(
  assetId: string,
  removed: Set<string>,
  heading: HTMLElement | null,
) {
  const tiles = Array.from(
    document.querySelectorAll<HTMLElement>("[data-asset-open]"),
  ).filter((tile) => !removed.has(tile.dataset.assetOpen ?? ""));
  (
    tiles.find((tile) => tile.dataset.assetOpen === assetId) ??
    tiles[0] ??
    heading
  )?.focus();
}

const DAY_S = 86_400;

export default function LibraryPage() {
  const viewer = useViewer();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { data: modelCatalog } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  // The viewer's own payload: in-progress work, their id and their apps.
  const { data: home } = useQuery({
    queryKey: ["account", "home"],
    queryFn: productApi.home,
    retry: false,
    refetchInterval: (query) =>
      inProgressMedia(query.state.data?.receipts).length > 0 ? 15_000 : false,
  });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const removedAssetIds = useRef(new Set<string>());
  const deletingAssetId = useRef<string | null>(null);
  const canChooseOwner = ["owner", "superadmin"].includes(viewer.role);
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
  const instanceView = canChooseOwner && owner !== "mine";
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
    // Results stay bound to the person filter that produced them: keep the old
    // grid while other filters change, but never across a change of person.
    placeholderData: (previous, previousQuery) =>
      (previousQuery?.queryKey[1] as { owner?: string } | undefined)?.owner ===
      owner
        ? previous
        : undefined,
    queryFn: ({ pageParam, signal }) =>
      mediaRequest<MediaResponse>(
        mediaPath +
          (pageParam ? `&cursor=${encodeURIComponent(String(pageParam))}` : ""),
        { signal },
      ),
    getNextPageParam: (last: MediaResponse) => last.next_cursor ?? undefined,
    retry: false,
  });
  const lastLoadedPages = useRef<{ owner: string; pages: MediaResponse[] }>({
    owner,
    pages: [],
  });
  useEffect(() => {
    if (data && !isPlaceholderData)
      lastLoadedPages.current = { owner, pages: data.pages };
  }, [data, isPlaceholderData, owner]);
  const mediaPages = useMemo(
    () =>
      data?.pages ??
      (isError && lastLoadedPages.current.owner === owner
        ? lastLoadedPages.current.pages
        : []),
    [data?.pages, isError, owner],
  );
  const assets = useMemo(() => {
    const listed = mediaPages
      .flatMap((page) => page.assets)
      .filter((item) => isMediaKind(item.kind));
    // A video's poster is listed as its own image; it is part of the video.
    const posters = new Set(
      listed.map((item) => item.poster_asset_id).filter(Boolean),
    );
    // The kind is known for every item, so a new kind applies at once, even
    // while the previous grid stands in for the request.
    return listed.filter(
      (item) =>
        !posters.has(item.asset_id) && (kind === "all" || item.kind === kind),
    );
  }, [mediaPages, kind]);
  const sources = useMemo(() => mediaPages[0]?.sources ?? [], [mediaPages]);
  const { people, lookup: personFor } = usePeople(canChooseOwner, sources);
  const viewerPerson =
    viewer.name && viewer.name !== "Your account" ? viewer.name : null;
  const hasLoadedAssets = assets.length > 0;
  const updating = isFetching && isPlaceholderData && hasLoadedAssets;
  const showInitialError = isError && !hasLoadedAssets && !isFetchNextPageError;
  const showRetainedError =
    (isError || isFetchNextPageError) && hasLoadedAssets;
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
  const storedAssets = useMemo(
    () => assets.filter((asset) => asset.available !== false),
    [assets],
  );
  const goneAssets = useMemo(
    () => assets.filter((asset) => asset.available === false),
    [assets],
  );
  // The viewer's own media requests still running. They show only where the
  // viewer's own work belongs and when no filter they cannot answer is set.
  const runningMedia = useMemo(
    () => inProgressMedia(home?.receipts),
    [home?.receipts],
  );
  const inProgress = useMemo(
    () =>
      ["default", "everyone", "mine"].includes(owner) &&
      model === "all" &&
      source === "all" &&
      !debouncedSearch.trim()
        ? runningMedia.filter((item) => kind === "all" || item.kind === kind)
        : [],
    [debouncedSearch, kind, model, owner, runningMedia, source],
  );
  // When running work finishes, its creation is now in the list.
  const runningCount = useRef(runningMedia.length);
  useEffect(() => {
    if (runningMedia.length < runningCount.current) void refetch();
    runningCount.current = runningMedia.length;
  }, [refetch, runningMedia.length]);
  // Owners can see how long the gateway keeps pictures (images.cache_ttl).
  const { data: gatewayStatus } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled: canChooseOwner,
    retry: false,
  });
  const retention = (
    gatewayStatus as
      | { budgets?: { images?: { cache_ttl?: number | null } } }
      | undefined
  )?.budgets?.images?.cache_ttl;
  const imageTtl =
    typeof retention === "number" && retention > 0 ? retention : null;
  const expiringImages = useMemo(() => {
    if (!imageTtl) return 0;
    const now = Date.now();
    return storedAssets.filter((asset) => {
      const created = asset.kind === "image" && toDateValue(asset.created_at);
      if (!created) return false;
      const expires = created.getTime() + imageTtl * 1000;
      return expires > now && expires <= now + 3 * DAY_S * 1000;
    }).length;
  }, [imageTtl, storedAssets]);
  const goneConfirmed = goneAssets.every((asset) =>
    confirmedGone(asset.unavailable_reason),
  );
  const [goneOpen, setGoneOpen] = useState(false);
  // Display names: the catalog, else a capability's recommended name (from
  // home, or the status payload if another page already loaded it).
  const cachedStatus = queryClient.getQueryData<MemberStatus>([
    "account",
    "status",
  ]);
  const nameCapabilities: CapabilityNameRow[] = [
    ...(Array.isArray(home?.capabilities) ? home.capabilities : []),
    ...(Array.isArray(cachedStatus?.capabilities)
      ? cachedStatus.capabilities
      : []),
  ];
  const nameModel: NameModel = (id) =>
    modelDisplayName(id, modelCatalog, nameCapabilities);
  // Delete only what the viewer made. In a person or "Everyone" view that must
  // be shown by the creation's app; if it cannot be, Delete is not offered.
  const viewerUserId = home?.user?.id ?? null;
  const viewerAppIds = new Set(
    (Array.isArray(home?.apps) ? home.apps : []).map((app) => app.id),
  );
  function madeByViewer(asset: MediaAsset) {
    if (!instanceView) return true;
    const appId =
      asset.source?.app_id ??
      (asset.source?.id?.startsWith("app:")
        ? asset.source.id.slice(4)
        : undefined);
    if (appId && viewerAppIds.has(appId)) return true;
    const ownerId = sources.find(
      (item) =>
        (appId && item.app_id === appId) ||
        (asset.source?.id && item.id === asset.source.id),
    )?.owner_user_id;
    return Boolean(viewerUserId && ownerId && ownerId === viewerUserId);
  }
  function returnFocus(assetId: string) {
    // After a delete, the confirmation still holds focus; the page returns
    // focus once it closes (below).
    if (deletingAssetId.current) return;
    focusTile(assetId, removedAssetIds.current, headingRef.current);
  }
  useEffect(() => {
    if (confirming || !deletingAssetId.current) return;
    const assetId = deletingAssetId.current;
    deletingAssetId.current = null;
    const timer = window.setTimeout(
      () => focusTile(assetId, removedAssetIds.current, headingRef.current),
      0,
    );
    return () => window.clearTimeout(timer);
  }, [confirming]);
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
    onSuccess: (_result, asset) => {
      removedAssetIds.current.add(asset.asset_id);
      toast.success("Deleted from your library.");
      closeAsset();
      void queryClient.invalidateQueries({ queryKey: ["library"] });
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Delete failed. Try again.",
      ),
  });
  const personOptions = canChooseOwner
    ? [
        { value: "everyone", label: "Everyone" },
        ...(viewerPerson
          ? [{ value: "mine", label: `${viewerPerson} (you)` }]
          : [{ value: "mine", label: "You" }]),
        ...people
          .filter((person) => person.name !== viewerPerson)
          .map((person) => ({ value: person.id, label: person.name })),
      ]
    : [];
  const ownerValue = owner === "default" ? "everyone" : owner;
  // Kind and person: in the control row on wide screens, inside Filters on a
  // phone. One function so both places stay the same control.
  const personPicker = (place: "row" | "sheet") => (
    <>
      {canChooseOwner ? (
        <Select
          value={ownerValue}
          onValueChange={(value) => {
            setOwner(value);
            setSource("all");
          }}
        >
          <SelectTrigger
            aria-label="Person"
            // Match the segmented control and Filters (40 px) above phone width.
            className={cn(
              "sm:data-[size=default]:h-10",
              place === "sheet" ? "w-full" : "w-auto min-w-40",
            )}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {personOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                <span className="flex items-center gap-2">
                  {option.value === "everyone" ? (
                    // The household at a glance: up to three people's marks.
                    <span className="flex -space-x-1">
                      {people.slice(0, 3).map((person) => (
                        <PersonMark
                          key={person.id}
                          name={person.name}
                          size="xs"
                          className="ring-card ring-2"
                        />
                      ))}
                    </span>
                  ) : (
                    <PersonMark
                      name={
                        option.value === "mine"
                          ? (viewerPerson ?? "You")
                          : option.label
                      }
                      size="xs"
                    />
                  )}
                  {option.label}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : null}
    </>
  );
  const selectedPerson = selected
    ? instanceView
      ? personFor(selected)
      : viewerPerson
    : null;
  return (
    <div>
      {/* The title and count, then one row of controls with search first
          (D1.3: search floated alone at the right, and on a phone four rows
          of controls filled half the screen). On a phone the row is search
          and one Filters control; kind and person move into it. The heading
          keeps a ref so the viewer can return focus to it. */}
      <header className="mb-4 flex min-h-10 items-center">
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="text-2xl font-semibold tracking-tight"
        >
          Library
          {hasLoadedAssets && !hasNextPage && !activeFilters ? (
            // The count is what the grid shows; files no longer stored are
            // counted beside it, not inside it (red team round 2).
            <span className="text-subtle-foreground figures ml-2.5 text-lg font-normal">
              {storedAssets.length}
            </span>
          ) : null}
        </h1>
        {!isLoading && !showInitialError && goneAssets.length > 0 ? (
          <button
            type="button"
            onClick={() => {
              setGoneOpen(true);
              document
                .getElementById("gone-files")
                ?.scrollIntoView({ block: "start" });
            }}
            className="text-link ml-3 min-h-11 text-sm font-medium underline-offset-4 hover:underline sm:min-h-0"
          >
            {goneAssets.length === 1
              ? goneConfirmed
                ? "1 no longer stored"
                : "1 unavailable"
              : goneConfirmed
                ? `${goneAssets.length} no longer stored`
                : `${goneAssets.length} unavailable`}
          </button>
        ) : null}
      </header>
      <section
        aria-label="Library filters"
        className="mb-6 flex flex-wrap items-center gap-2"
      >
        <label className="relative block min-w-0 max-sm:flex-1 sm:w-64">
          <span className="sr-only">Search your creations</span>
          <Search
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search your creations"
            className="pl-9"
          />
        </label>
        {/* The kind switch stays one tap away on a phone too (D1.4: audio
            was buried behind Filters); there it takes its own line. */}
        <Segmented
          label="Kind"
          className="max-sm:order-last max-sm:w-full"
          value={kind}
          onChange={setKind}
          options={[
            { value: "all", label: "All" },
            { value: "image", label: "Images", icon: ImageIcon },
            { value: "video", label: "Video", icon: Video },
            { value: "audio", label: "Audio", icon: AudioLines },
          ]}
        />
        <div className="contents max-sm:hidden">{personPicker("row")}</div>
        <MoreFilters
          model={model}
          setModel={setModel}
          models={models}
          date={date}
          setDate={setDate}
          source={source}
          setSource={setSource}
          sources={sources}
          instanceView={instanceView}
          nameModel={nameModel}
          phoneExtras={canChooseOwner ? personPicker("sheet") : undefined}
          phoneActive={owner !== "default" ? 1 : 0}
          onClear={activeFilters ? clearFilters : undefined}
        />
        {activeFilters ? (
          <Button
            variant="ghost"
            onClick={clearFilters}
            className="max-sm:hidden"
          >
            Clear filters
          </Button>
        ) : null}
      </section>
      {deepLinkedAssetError ? (
        <div className="mb-4">
          <ErrorState
            heading="Couldn’t open that creation"
            message="That library item could not be loaded. Try again."
            onRetry={() => void refetchDeepLinkedAsset()}
            retrying={deepLinkedAssetFetching}
          />
        </div>
      ) : null}
      {isLoading ? (
        <div
          role="status"
          aria-label="Loading library"
          aria-busy="true"
          className="space-y-3"
        >
          <Skeleton className="h-5 w-24" />
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
            {Array.from({ length: 12 }).map((_, index) => (
              <Skeleton key={index} className="rounded-media aspect-square" />
            ))}
          </div>
        </div>
      ) : null}
      {showInitialError ? (
        <ErrorState
          heading="Couldn’t load your library"
          message={null}
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : null}
      {!isLoading &&
      !showInitialError &&
      assets.length === 0 &&
      inProgress.length === 0 ? (
        <EmptyState
          icon={activeFilters ? Search : ImageIcon}
          title={activeFilters ? "No matching creations" : "No creations yet"}
          description={
            activeFilters
              ? "Clear filters to see more."
              : "Images, video and audio made through the gateway appear here."
          }
          action={
            activeFilters ? (
              <Button variant="outline" onClick={clearFilters}>
                Clear filters
              </Button>
            ) : (
              <Button asChild>
                <a href="/ui/try/?capability=image">Make an image</a>
              </Button>
            )
          }
        />
      ) : null}
      {!isLoading && !showInitialError && expiringImages > 0 ? (
        <p className="mb-4 text-sm">
          {expiringImages === 1
            ? "1 picture will be removed within 3 days. Download it to keep it."
            : `${expiringImages} pictures will be removed within 3 days. Download the ones to keep.`}
        </p>
      ) : null}
      {!isLoading &&
      !showInitialError &&
      (storedAssets.length > 0 || inProgress.length > 0) ? (
        <div aria-busy={updating} className="space-y-7">
          {/* One continuous grid, newest first; each tile's caption carries
              its date, so no part-filled week group leaves a gap. */}
          <div className="grid grid-cols-3 gap-1 sm:gap-1.5 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
            {inProgress.map((item) => (
              <InProgressTile key={item.id} item={item} />
            ))}
            {storedAssets.map((asset) => (
              <AssetTile
                key={asset.asset_id}
                asset={asset}
                onOpen={openAsset}
                everyone={instanceView}
                person={instanceView ? personFor(asset) : null}
                selected={selected?.asset_id === asset.asset_id}
              />
            ))}
          </div>
        </div>
      ) : null}
      {!isLoading && !showInitialError && goneAssets.length > 0 ? (
        <section
          id="gone-files"
          aria-label={
            goneConfirmed ? "Files no longer stored" : "Unavailable files"
          }
          className={cn(storedAssets.length > 0 && "mt-10 border-t pt-6")}
        >
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <p className="text-sm" role="status">
              {goneAssets.length === 1
                ? goneConfirmed
                  ? "1 file is no longer stored."
                  : "1 file is unavailable."
                : goneConfirmed
                  ? `${goneAssets.length} files are no longer stored.`
                  : `${goneAssets.length} files are unavailable.`}{" "}
              {/* One explanation for owners and members alike. */}
              <span className="text-muted-foreground">
                Their prompts and receipts remain.
              </span>
            </p>
            <Button
              variant="link"
              className="h-auto px-0"
              onClick={() => void refetch()}
            >
              Refresh library
            </Button>
          </div>
          <details
            className="group mt-2"
            open={goneOpen}
            onToggle={(event) => setGoneOpen(event.currentTarget.open)}
          >
            <summary className="text-link flex min-h-11 w-fit cursor-pointer items-center text-sm font-medium sm:min-h-8">
              Show them
            </summary>
            <div className="mt-3 grid grid-cols-3 gap-1.5 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8">
              {goneAssets.map((asset) => (
                <AssetTile
                  key={asset.asset_id}
                  asset={asset}
                  onOpen={openAsset}
                  everyone={instanceView}
                  person={instanceView ? personFor(asset) : null}
                  selected={selected?.asset_id === asset.asset_id}
                />
              ))}
            </div>
          </details>
        </section>
      ) : null}
      {updating ? (
        <p role="status" className="text-muted-foreground mt-3 text-sm">
          Updating
        </p>
      ) : null}
      {showRetainedError ? (
        <div
          role="alert"
          className="border-destructive/30 bg-danger-tint text-destructive rounded-panel mt-6 flex flex-wrap items-center justify-center gap-3 border p-3 text-sm"
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
        <div className="mt-8 flex justify-center">
          <Button
            variant="outline"
            disabled={isFetchingNextPage}
            onClick={() => void fetchNextPage()}
          >
            {isFetchingNextPage ? "Loading more…" : "Load more"}
          </Button>
        </div>
      ) : null}
      {selected ? (
        <CreationViewer
          asset={selected}
          assets={assets}
          onSelect={openAsset}
          onClose={closeAsset}
          everyone={instanceView}
          nameModel={nameModel}
          person={selectedPerson}
          downloading={downloadingAssetId === selected.asset_id}
          onDownload={(asset) => {
            setDownloadingAssetId(asset.asset_id);
            void prepareDownload(asset, instanceView).then(
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
          onDelete={
            madeByViewer(selected) ? (asset) => setConfirming(asset) : null
          }
          onReturnFocus={returnFocus}
        />
      ) : null}
      <ConfirmDialog
        open={Boolean(confirming)}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={`Delete ${confirming ? assetTitle(confirming) : "this creation"}?`}
        description="This removes the creation from your library. Copies you downloaded are not affected."
        confirmLabel="Delete"
        onConfirm={async () => {
          if (!confirming) return;
          deletingAssetId.current = confirming.asset_id;
          try {
            await deleteMutation.mutateAsync(confirming);
          } catch (error) {
            deletingAssetId.current = null;
            throw error;
          }
        }}
      />
    </div>
  );
}
