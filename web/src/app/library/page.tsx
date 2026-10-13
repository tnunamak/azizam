"use client";

import Link from "next/link";
import { JobResult } from "@/components/jobs/job-notices";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import NextImage from "next/image";
import { flushSync } from "react-dom";
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
  EyeOff,
  FileDown,
  ImageIcon,
  ListFilter,
  MoreHorizontal,
  Pause,
  Play,
  ReceiptText,
  Search,
  Share,
  Trash2,
  Video,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { toast } from "@/lib/toast";

import { cn } from "@/lib/cn";
import { formatAbsoluteUTC } from "@/lib/format";
import { MakingTile } from "@/components/product/making-tile";
import { RelativeTime } from "@/components/product/relative-time";
import { ErrorState } from "@/components/product/error-state";
import { EmptyState } from "@/components/product/empty-state";
import { PersonMark } from "@/components/product/person-mark";
import {
  KeyValueList,
  type KeyValueItem,
} from "@/components/product/key-value";
import {
  AudioFace,
  DayChip,
  mediaGridGap,
  tilePlayButton,
  TileMeta,
  Waveform,
} from "@/components/product/audio-cover";
import { useAudioPeaks } from "@/lib/audio-peaks";
import { Segmented } from "@/components/product/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import { safeModelId } from "@/lib/model-name";
import { markCreationOpened } from "@/components/jobs/opened-creations";
import { api, queryKeys } from "@/lib/api";
import { accountApi } from "@/lib/account-api";
import {
  promptMemoryKey,
  promptNotSavedReason,
  tryKeepState,
} from "@/lib/prompt-memory";
import {
  dayMarks,
  inProgressMedia,
  modelDisplayName,
  tileDate,
  captionTitle,
  type CapabilityNameRow,
} from "./library-format";

type NameModel = (model: string, workflow?: string | null) => string;

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
  /** A small WebP preview (about 480 px). Tiles show it; the viewer uses the original. */
  thumbnail_url?: string | null;
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
  /** `person_id` is the maker, when the gateway knows who made it. */
  source?: {
    id?: string;
    kind: string;
    label: string;
    app_id?: string;
    person_id?: string;
  };
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

/**
 * A creation's name when the viewer has no prompt for it. The gateway gives
 * a prompt only to the person who made the item, and only while it is kept
 * (docs/design/PROMPT_MEMORY.md); apps' keys and "Don't keep this prompt"
 * keep none. Such items are named by type and time, "Video, Oct 20 at 7:45
 * PM", never "Image" alone (D1.6); the maker is beside the name in the
 * viewer and on hover.
 */
function untitledName(asset: MediaAsset): string {
  const date = toDateValue(asset.created_at);
  const word = { image: "Picture", video: "Video", audio: "Audio" }[asset.kind];
  if (!date) return word;
  const day = date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
  const time = date.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
  return `${word}, ${day} at ${time}`;
}

function assetTitle(asset: MediaAsset) {
  const prompt = typeof asset.prompt === "string" ? asset.prompt.trim() : "";
  return prompt || untitledName(asset);
}

/** A heading-length title: the prompt's first clause, or its first words. */
function shortTitle(asset: MediaAsset): string {
  const prompt = typeof asset.prompt === "string" ? asset.prompt.trim() : "";
  if (!prompt) return untitledName(asset);
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
}, everyoneByDefault: boolean) {
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
  // The API starts everyone on their own work, so the owner's default
  // "Everyone" view asks for it.
  if (filters.owner === "everyone" || (filters.owner === "default" && everyoneByDefault))
    params.set("scope", "everyone");
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

type LibraryStage = "library" | "prepare" | "download" | "share" | "delete";
class LibraryFailure extends Error {
  constructor(
    readonly stage: LibraryStage,
    readonly reason: "gateway" | "unreadable" | "unknown",
  ) {
    super(reason === "unknown"
      ? "We couldn't tell where this stopped. Try again."
      : reason === "unreadable"
        ? "The gateway returned an unreadable response. Try again."
        : stage === "library"
          ? "The gateway couldn't load your library. Try again."
          : stage === "prepare"
            ? "The gateway couldn't prepare this file. Try again."
            : stage === "download"
              ? "The file couldn't be downloaded. Try again."
              : stage === "delete"
                ? "The gateway couldn't delete this creation. Try again."
                : "This browser couldn't share the file. Use Download.");
  }
}
function failureCopy(error: unknown) {
  return error instanceof LibraryFailure
    ? error.message
    : "We couldn't tell where this stopped. Try again.";
}
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function validAsset(value: unknown): boolean {
  if (!record(value)) return false;
  return typeof value.asset_id === "string" && !!value.asset_id &&
    typeof value.kind === "string" && isMediaKind(value.kind) &&
    (value.prompt == null || typeof value.prompt === "string") &&
    (value.model == null || typeof value.model === "string") &&
    (value.available == null || typeof value.available === "boolean") &&
    ["download_url", "thumbnail_url", "workflow_download_url", "poster_asset_id", "request_id", "unavailable_reason", "owner_name"]
      .every((key) => value[key] == null || typeof value[key] === "string") &&
    (value.created_at == null || typeof value.created_at === "string" || typeof value.created_at === "number") &&
    (value.media_type == null || typeof value.media_type === "string") &&
    (value.source == null || (record(value.source) &&
      typeof value.source.kind === "string" && typeof value.source.label === "string" &&
      ["id", "app_id", "person_id"].every((key) => value.source && record(value.source) &&
        (value.source[key] == null || typeof value.source[key] === "string"))));
}
function validPayload(path: string, value: unknown): boolean {
  if (!record(value)) return false;
  const route = path.split("?")[0];
  if (route.endsWith("/download"))
    return typeof value.download_url === "string" && !!value.download_url.trim();
  if (route === "/me/api/session")
    return typeof value.csrf_token === "string" && !!value.csrf_token;
  if (route === "/me/api/media")
    return Array.isArray(value.assets) && value.assets.every(validAsset) &&
      (value.next_cursor == null || typeof value.next_cursor === "string") &&
      (value.models == null || (Array.isArray(value.models) && value.models.every((model) => typeof model === "string"))) &&
      (value.sources == null || (Array.isArray(value.sources) && value.sources.every((source) =>
        record(source) && typeof source.id === "string" && typeof source.label === "string")));
  if (route.startsWith("/me/api/receipts/"))
    return ["resolved_workflow", "resolved_model", "requested_model"].every((key) =>
      value[key] == null || typeof value[key] === "string") &&
      (value.remote == null || typeof value.remote === "boolean") &&
      (value.cost == null || (record(value.cost) &&
        (value.cost.usd == null || typeof value.cost.usd === "number" || typeof value.cost.usd === "string")));
  if (route.startsWith("/me/api/media/")) return validAsset(value.asset);
  return false;
}

async function mediaRequest<T>(
  path: string,
  init: RequestInit = {},
  stage: LibraryStage = "library",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch {
    throw new LibraryFailure(stage, "unknown");
  }
  if (!response.ok) throw new LibraryFailure(stage, "gateway");
  if (init.method === "DELETE" && response.status === 204) return undefined as T;
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new LibraryFailure(stage, "unreadable");
  }
  if (init.method === "DELETE" ? !record(payload) : !validPayload(path, payload))
    throw new LibraryFailure(stage, "unreadable");
  // Missing model names remain unknown; string consumers receive an empty value.
  // Sizes and times come from free-form job metadata: a value that is not a
  // finite number is unknown, not a reason to refuse the whole library.
  if (record(payload)) {
    const assets = Array.isArray(payload.assets) ? payload.assets : record(payload.asset) ? [payload.asset] : [];
    for (const asset of assets) {
      if (!record(asset)) continue;
      if (asset.model == null) asset.model = "";
      for (const key of ["width", "height", "generation_ms", "duration_s"]) {
        const number = typeof asset[key] === "string" && asset[key].trim() ? Number(asset[key]) : asset[key];
        asset[key] = typeof number === "number" && Number.isFinite(number) ? number : null;
      }
    }
  }
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
  retrying = false,
}: {
  kind: MediaKind;
  copy: string;
  retrying?: boolean;
  onRetry?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  const Icon = kindIcon(kind);
  return (
    <div className="bg-muted text-muted-foreground flex h-full flex-col items-center justify-center gap-0.5 p-2 text-center sm:gap-2 sm:p-3">
      <div role="img" aria-label={`${kindLabel(kind)} preview unavailable`}>
        <Icon aria-hidden="true" className="mx-auto size-4 sm:size-5" />
      </div>
      <p className="text-badge font-normal">{copy}</p>
      {onRetry ? (
        <Button
          type="button"
          variant="link"
          size="xs"
          className="relative z-20 max-sm:min-h-11"
          onClick={onRetry}
          disabled={retrying}
          aria-busy={retrying || undefined}
        >
          {retrying ? "Loading…" : "Retry"}
        </Button>
      ) : null}
    </div>
  );
}

/**
 * `withDetails` asks for the maker's copy with the prompt and settings
 * embedded; the gateway refuses it to anyone else (HTTP 403).
 */
async function prepareDownload(
  asset: MediaAsset,
  everyone: boolean,
  withDetails = false,
) {
  const path = `/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=${everyone ? "everyone" : "me"}${withDetails ? "&metadata=full" : ""}`;
  const payload = await mediaRequest<{ download_url: string }>(path, {}, "prepare");
  const link = document.createElement("a");
  link.href = payload.download_url;
  link.download = assetTitle(asset);
  link.click();
}

/**
 * Share the file itself where the browser can (a phone's share sheet), so
 * "send the birthday picture to the family" is one tap (red team round 2).
 * Falls back to sharing the creation's title alone if files cannot be
 * shared; the signed link would expire, so it is never shared.
 */
async function shareCreation(asset: MediaAsset, everyone: boolean) {
  const path = `/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=${everyone ? "everyone" : "me"}`;
  const payload = await mediaRequest<{ download_url: string }>(path, {}, "prepare");
  let media: Response;
  try {
    media = await fetch(payload.download_url);
  } catch {
    throw new LibraryFailure("download", "unknown");
  }
  if (!media.ok) throw new LibraryFailure("download", "gateway");
  // An error page (HTML, JSON) must not become a shared "file". A generic
  // binary type falls back to the gateway's declared media type.
  const header = media.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  const declared = asset.media_type?.toLowerCase();
  const type = header?.startsWith(`${asset.kind}/`)
    ? header
    : (!header || header === "application/octet-stream") && declared?.startsWith(`${asset.kind}/`)
      ? declared
      : null;
  if (!type) throw new LibraryFailure("download", "unreadable");
  let blob: Blob;
  try {
    blob = await media.blob();
  } catch {
    throw new LibraryFailure("download", "unknown");
  }
  const extension = type.split("/")[1]?.split(";")[0] ?? "bin";
  const file = new File([blob], `${shortTitle(asset)}.${extension}`, {
    type,
  });
  try {
    if (!navigator.canShare?.({ files: [file] }))
      throw new LibraryFailure("share", "gateway");
    await navigator.share({ files: [file], title: shortTitle(asset) });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new LibraryFailure("share", "gateway");
  }
}

function absolute(ts: MediaAsset["created_at"]) {
  return ts ? formatAbsoluteUTC(ts) : undefined;
}

function pauseOtherAudio(current: HTMLAudioElement) {
  current.ownerDocument.querySelectorAll("audio").forEach((audio) => {
    if (audio !== current) audio.pause();
  });
}

/**
 * An audio creation has no picture, so its tile is a calm neutral face with
 * its own cover and title. One persistent control groups play, type and
 * length, leaving room for the day chip without adding another overlay.
 */
/** The file a signed URL points at. A refetch re-signs the query, not the file. */
function mediaResource(url: string) {
  try {
    const parsed = new URL(url, "http://resource.invalid");
    return parsed.protocol === "data:" ? url : parsed.origin + parsed.pathname;
  } catch {
    return url;
  }
}

function AudioTile({
  assetId,
  src,
  title,
  duration,
  visible,
}: {
  assetId: string;
  src: string;
  title: string;
  duration?: number | null;
  visible: boolean;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [measuredDuration, setMeasuredDuration] = useState<number | null>(
    duration ?? null,
  );
  const [error, setError] = useState<"media" | "playback" | null>(null);

  useEffect(() => {
    const audio = audioRef.current;
    return () => {
      if (!audio) return;
      if (audio.isConnected) audio.pause();
      audio.removeAttribute("src");
    };
  }, []);

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
    <>
      <AudioFace
        peaksKey={assetId}
        src={src}
        visible={visible}
        status={errorText || buffering ? <span role="status">{errorText || "Loading audio…"}</span> : null}
      />
      <audio
        ref={audioRef}
        preload="none"
        // "waiting" means playback needs data; preload="none" never
        // reaches canplay, so loadstart would show a wait that never ends.
        onWaiting={() => setBuffering(true)}
        onCanPlay={() => setBuffering(false)}
        onPlaying={() => setBuffering(false)}
        onLoadedMetadata={(event) => {
          setError(null);
          setMeasuredDuration(event.currentTarget.duration);
        }}
        onPlay={(event) => {
          pauseOtherAudio(event.currentTarget);
          setError(null);
          setIsPlaying(true);
        }}
        onPause={() => {
          setIsPlaying(false);
          setBuffering(false);
        }}
        onEnded={() => setIsPlaying(false)}
        onError={() => {
          setIsPlaying(false);
          setBuffering(false);
          setError("media");
        }}
      />
      <button
        type="button"
        data-tile-cue="playback"
        className={tilePlayButton}
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
        {isPlaying ? (
          <Pause aria-hidden className="size-4 fill-current" />
        ) : (
          <Play aria-hidden className="size-4 fill-current" />
        )}
        <TileMeta icon={AudioLines}>{durationText}</TileMeta>
      </button>
    </>
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
  const [buffering, setBuffering] = useState(false);
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
        onWaiting={() => setBuffering(true)}
        onCanPlay={() => setBuffering(false)}
        onPlaying={() => setBuffering(false)}
        onError={() => { setBuffering(false); onFailed(); }}
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
          className="text-foreground h-14 w-full flex-none"
        />
      ) : null}
      <div className="flex w-full items-center gap-3">
        <span className="figures text-muted-foreground text-badge w-10 text-right">
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
          className="accent-primary h-10 min-w-0 flex-1 cursor-pointer max-sm:h-11"
        />
        <span className="figures text-muted-foreground text-badge w-10">
          {length != null ? formatDuration(length) : "–:––"}
        </span>
      </div>
      {buffering ? <p role="status">Loading audio…</p> : null}
      {blocked ? (
        <p role="status" className="text-meta">
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
  const [buffering, setBuffering] = useState(false);
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
        onWaiting={() => setBuffering(true)}
        onCanPlay={() => setBuffering(false)}
        onPlaying={() => setBuffering(false)}
        onError={() => { setBuffering(false); onFailed(); }}
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
          className="size-10 rounded-full max-sm:size-11"
          aria-label={`${playing ? "Pause" : "Play"} ${title}`}
          onClick={() => void toggle()}
        >
          {playing ? <Pause aria-hidden /> : <Play aria-hidden />}
        </Button>
        <span className="figures text-badge w-10 text-right">
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
          className="accent-primary h-10 min-w-0 flex-1 cursor-pointer max-sm:h-11"
        />
        <span className="figures text-badge w-10">
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
      {buffering ? <p role="status">Loading video…</p> : null}
      {blocked ? (
        <p role="status" className="text-chrome-foreground text-meta px-3 pb-2">
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
  const [thumbFailed, setThumbFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [retrySrc, setRetrySrc] = useState<string | null>(null);
  const [recovered, setRecovered] = useState(false);
  const retryPending = useRef(false);
  const [retrying, setRetrying] = useState(false);
  const tileVideoRef = useRef<HTMLVideoElement>(null);
  const [tilePlaying, setTilePlaying] = useState(false);
  const [tileStarted, setTileStarted] = useState(false);
  const [tileBlocked, setTileBlocked] = useState(false);
  const [tileBuffering, setTileBuffering] = useState(false);
  const [tileDuration, setTileDuration] = useState(asset.duration_s);
  const {
    data: poster,
    isPending: posterPending,
    isError: posterLoadFailed,
    refetch: refetchPoster,
  } = useQuery({
    queryKey: ["library", "poster", asset.poster_asset_id, everyone],
    // Off-screen tiles wait: one poster link per video would flood a large Library.
    enabled: asset.kind === "video" && !!asset.poster_asset_id && visible && !asset.thumbnail_url,
    queryFn: () =>
      mediaRequest<{ download_url: string }>(
        `/me/api/media/${encodeURIComponent(asset.poster_asset_id!)}/download?scope=${everyone ? "everyone" : "me"}`,
      ),
    retry: false,
  });

  // The server's small preview stands in as the video poster too: no
  // per-tile poster link, and no video bytes until the person presses play.
  const thumbUrl = asset.thumbnail_url && !thumbFailed && !retrySrc ? asset.thumbnail_url : undefined;
  const posterUrl = thumbUrl ?? poster?.download_url;
  const effectivePosterUrl = posterFailed ? undefined : posterUrl;
  const currentSrc = retrySrc ?? src;
  const waitingForPoster =
    asset.kind === "video" && !!asset.poster_asset_id && !thumbUrl && posterPending;
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
        retrying={retrying}
        onRetry={(event) => {
          event.stopPropagation();
          if (retryPending.current) return;
          retryPending.current = true;
          setRetrying(true);
          // Try the current address again while a fresh link is fetched.
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
          ).finally(() => {
            retryPending.current = false;
            setRetrying(false);
          });
          void queryClient.invalidateQueries({ queryKey: ["library"] });
        }}
      />
    );
  if (asset.kind === "image" && currentSrc) {
    // Tiles load the small preview; the viewer and any retry load the original.
    const imageSrc = !large && thumbUrl ? thumbUrl : currentSrc;
    return (
      <NextImage
        key={`image-${asset.asset_id}-${retryKey}-${imageSrc}`}
        unoptimized
        loading="lazy"
        decoding="async"
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={imageSrc}
        alt={assetTitle(asset)}
        onError={() => (imageSrc === thumbUrl ? setThumbFailed(true) : setMediaFailed(true))}
        className={cn(
          "h-full w-full",
          large ? "object-contain" : "object-cover",
        )}
      />
    );
  }
  if (
    asset.kind === "video" &&
    currentSrc &&
    (!asset.poster_asset_id || !waitingForPoster)
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
      <>
        <video
          ref={tileVideoRef}
          key={`video-${asset.asset_id}-${retryKey}`}
          src={tileStarted || (visible && !effectivePosterUrl) ? `${currentSrc}#t=0.1` : undefined}
          poster={effectivePosterUrl}
          preload="none"
          muted
          playsInline
          onWaiting={() => setTileBuffering(true)}
          onCanPlay={() => setTileBuffering(false)}
          onPlaying={() => setTileBuffering(false)}
          onLoadedMetadata={(event) => {
            const video = event.currentTarget;
            if (Number.isFinite(video.duration) && video.duration >= 0) {
              setTileDuration(video.duration);
              if (!effectivePosterUrl && !tileStarted && video.duration > 0)
                video.currentTime = Math.min(0.1, video.duration / 2);
            }
          }}
          onPlay={(event) => {
            pauseOtherAudio(event.currentTarget);
            setTilePlaying(true);
            setTileStarted(true);
            setTileBlocked(false);
          }}
          onPause={() => setTilePlaying(false)}
          onEnded={() => setTilePlaying(false)}
          onError={() => {
            setTilePlaying(false);
            setTileBuffering(false);
            setMediaFailed(true);
            setTileBlocked(true);
          }}
          className="bg-media-stage h-full w-full object-cover"
          aria-label={assetTitle(asset)}
        />
        {effectivePosterUrl && !tileStarted ? (
          <NextImage
            unoptimized
            loading="lazy"
            decoding="async"
            width={asset.width ?? 1024}
            height={asset.height ?? 768}
            src={effectivePosterUrl}
            alt={assetTitle(asset)}
            onError={() => (effectivePosterUrl === thumbUrl ? setThumbFailed(true) : setPosterFailed(true))}
            className="absolute inset-0 h-full w-full object-cover"
          />
        ) : null}
        <button
          type="button"
          data-tile-cue="playback"
          className={tilePlayButton}
          aria-label={`${tilePlaying ? "Pause" : "Play"} ${assetTitle(asset)}`}
          onClick={async (event) => {
            event.stopPropagation();
            const video = tileVideoRef.current;
            if (!video) return;
            if (tilePlaying) {
              video.pause();
              setTilePlaying(false);
              return;
            }
            // React owns the source. Setting it here as well would write src
            // twice, and the second write reloads the media and aborts play().
            if (!tileStarted) flushSync(() => setTileStarted(true));
            if (mediaFailed) video.load();
            try {
              video.muted = false;
              await video.play();
              setTileStarted(true);
              setTilePlaying(true);
              setTileBlocked(false);
              setMediaFailed(false);
            } catch (error) {
              // Pausing while play() is pending rejects with AbortError; that is not a failure.
              if (error instanceof DOMException && error.name === "AbortError")
                return;
              setTilePlaying(false);
              setTileBlocked(true);
            }
          }}
        >
          {tilePlaying ? (
            <Pause aria-hidden className="size-4 fill-current" />
          ) : (
            <Play aria-hidden className="size-4 fill-current" />
          )}
          <TileMeta icon={Video}>
            {tileDuration != null && Number.isFinite(tileDuration) && tileDuration >= 0
              ? formatDuration(tileDuration)
              : null}
          </TileMeta>
        </button>
        {tileBuffering ? <p role="status" className="bg-media-badge text-media-badge-foreground absolute inset-x-2 top-2">Loading video…</p> : null}
        {tileBlocked ? (
          <p role="status" className="bg-media-badge text-media-badge-foreground text-badge absolute inset-x-2 top-10 z-30 rounded-badge px-2">
            {mediaFailed ? "Preview unavailable" : "Playback didn't start. Try again."}
          </p>
        ) : null}
      </>
    );
  if (asset.kind === "video" && effectivePosterUrl)
    return (
      <NextImage
        unoptimized
        loading="lazy"
        decoding="async"
        width={asset.width ?? 1024}
        height={asset.height ?? 768}
        src={effectivePosterUrl}
        alt={assetTitle(asset)}
        onError={() => (effectivePosterUrl === thumbUrl ? setThumbFailed(true) : setPosterFailed(true))}
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
  const {
    data: users,
    isError,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: queryKeys.users,
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
  return { people, lookup, query: { isError, isFetching, refetch } };
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

function AssetTile({
  asset,
  onOpen,
  everyone,
  person,
  selected,
  day,
}: {
  asset: MediaAsset;
  onOpen: (asset: MediaAsset) => void;
  everyone: boolean;
  person: string | null;
  selected: boolean;
  /** The day label, on the first tile of each day only. */
  day?: string | null;
}) {
  const { ref: visibilityRef, visible } = usePreviewVisible();
  const [makerTooltip, setMakerTooltip] = useState(false);
  const playableAudio =
    asset.kind === "audio" && asset.download_url && asset.available !== false;
  const hoverTitle = playableAudio
    ? null
    : captionTitle(asset.prompt, untitledName(asset), 56);
  // The first tile of a day already wears the day chip.
  const hoverDate = day
    ? null
    : tileDate(toDateValue(asset.created_at), new Date());
  return (
    <article
      className="group relative min-w-0"
      onMouseEnter={() => setMakerTooltip(true)}
      onMouseLeave={(event) =>
        setMakerTooltip(event.currentTarget.contains(document.activeElement))
      }
      onFocus={() => setMakerTooltip(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget))
          setMakerTooltip(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") setMakerTooltip(false);
      }}
    >
      <button
        type="button"
        data-asset-open={asset.asset_id}
        onClick={() => onOpen(asset)}
        className="rounded-media absolute inset-0 z-10"
        aria-label={`Open ${assetTitle(asset)}`}
        aria-describedby={
          everyone && person && makerTooltip ? `maker-${asset.asset_id}` : undefined
        }
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
            key={mediaResource(asset.download_url!)}
            assetId={asset.asset_id}
            src={asset.download_url!}
            title={assetTitle(asset)}
            duration={asset.duration_s}
            visible={visible}
          />
        ) : (
          <Preview
            key={`preview-${asset.asset_id}-${asset.download_url}`}
            asset={asset}
            everyone={everyone}
            visible={visible}
          />
        )}
        {/* Play, type and duration form one control; the day is the second
            persistent overlay. Maker and supplemental facts remain on focus. */}
        {/* D1.6: the day on the first tile of each day, and the maker when
            the whole household is shown; the grid stays continuous. */}
        {day ? (
          <span data-tile-cue="day" className="pointer-events-none absolute top-2 left-2 z-20">
            <DayChip>{day}</DayChip>
          </span>
        ) : null}
        {everyone && person ? (
          <PersonMark
            name={person}
            size="2xs"
            label={person}
            className="ring-media-badge absolute top-2 right-2 z-20 ring-2 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100"
          />
        ) : null}
        {everyone && person && makerTooltip ? (
          <span
            id={`maker-${asset.asset_id}`}
            role="tooltip"
            className="bg-media-badge text-media-badge-foreground text-badge pointer-events-none absolute top-8 right-2 z-30 max-w-full rounded-badge px-2 py-1"
          >
            Made by {person}
          </span>
        ) : null}
        {/* D1.4: always-on captions were cut to two or three words and the
            maker badge sat on every tile. The grid now shows media only; on
            a wide screen, hover or keyboard focus shows a short title (whole
            words, two lines) and the day. The maker is the mark and its
            tooltip, and the day is not repeated where its chip shows. The
            button's name carries the full prompt for screen readers. */}
        {hoverTitle || hoverDate ? (
          <div
            aria-hidden="true"
            className={cn(
              "dark bg-card/90 text-foreground pointer-events-none absolute inset-x-0 z-20 px-2.5 py-2 opacity-0 transition-opacity duration-(--motion-fast) group-focus-within:opacity-100 group-hover:opacity-100 max-sm:hidden",
              asset.kind === "image" ? "bottom-0" : "bottom-14",
            )}
          >
            {hoverTitle ? (
              <p className="text-badge line-clamp-2">{hoverTitle}</p>
            ) : null}
            {hoverDate ? (
              <p className="text-foreground/80 text-badge mt-0.5 font-normal">
                {hoverDate}
              </p>
            ) : null}
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
          "bg-primary text-primary-foreground figures rounded-badge text-badge size-5 items-center justify-center",
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
        <Button
          variant="outline"
          className="relative max-sm:size-11 max-sm:px-0"
        >
          <ListFilter aria-hidden="true" />
          <span className="max-sm:sr-only">Filters</span>
          {/* A phone counts the person too, which lives in here. */}
          {badge(
            active + phoneActive,
            "absolute -top-1.5 -right-1.5 flex sm:hidden",
          )}
          {badge(active, "max-sm:hidden sm:flex")}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 space-y-4">
        {phoneExtras ? (
          // Labelled like the filters under it (D1.5).
          <div className="space-y-1.5 sm:hidden">
            <p className="text-label">Person</p>
            {phoneExtras}
          </div>
        ) : null}
        <div className="space-y-1.5">
          <p className="text-label">Model</p>
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
          <p className="text-label">Date</p>
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
            <p className="text-label">App</p>
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
  resolved_workflow?: string | null;
  resolved_model?: string | null;
  requested_model?: string | null;
  remote?: boolean | null;
  cost?: { cost_class?: string; usd?: string | number | null } | null;
}

function costText(receipt: ReceiptCost): string {
  const recorded = receipt.cost?.usd;
  const usd = typeof recorded === "number" || (typeof recorded === "string" && recorded.trim())
    ? Number(recorded) : NaN;
  if (Number.isFinite(usd) && usd > 0)
    return usd < 0.01 ? "Less than USD 0.01" : `USD ${usd.toFixed(2)}`;
  if (receipt.remote === false && receipt.cost?.cost_class === "local")
    return "Local · no provider charge";
  // A recorded zero is a fact; a missing amount is not zero.
  if (usd === 0) return "USD 0.00";
  return "Cost not recorded";
}

/** The facts that have a value, for a KeyValueList. */
function presentFacts(
  facts: [key: string, value: ReactNode][],
): KeyValueItem[] {
  return facts.filter(([, value]) => value != null && value !== "");
}

/**
 * A supporting query's failure, with its own Retry. Its wait and an empty
 * answer stay quiet (P1): the page works without them.
 */
function QueryFeedback({ label, query }: {
  label: string;
  query: { isFetching: boolean; isError: boolean; refetch: () => unknown };
}) {
  if (!query.isError) return null;
  return (
    <p role="status" className="text-meta">
      The gateway couldn’t load {label}.{" "}
      <Button variant="link" disabled={query.isFetching} onClick={() => void query.refetch()}>
        {query.isFetching ? "Loading…" : `Retry ${label}`}
      </Button>
    </p>
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
  onShare,
  maker,
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
  onDownload: (asset: MediaAsset, withDetails?: boolean) => void;
  onShare: (asset: MediaAsset) => void;
  /**
   * The viewer made this item. Only the maker learns whether a prompt was
   * kept; anyone else sees no prompt and no sign that one exists.
   */
  maker: boolean;
  onDelete: ((asset: MediaAsset) => void) | null;
  /** Called with the creation shown last when the viewer closes. */
  onReturnFocus: (assetId: string) => void;
}) {
  const { data: renewed, isFetching: renewing, isError: renewalFailed, refetch: renew } = useQuery({
    queryKey: ["library", "download", asset.asset_id, everyone],
    queryFn: () => mediaRequest<{ download_url: string }>(
      `/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=${everyone ? "everyone" : "me"}`,
    ),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const index = assets.findIndex((item) => item.asset_id === asset.asset_id);
  const previous = index > 0 ? assets[index - 1] : null;
  const next =
    index >= 0 && index < assets.length - 1 ? assets[index + 1] : null;
  const {
    data: receipt,
    isPending: receiptPending,
    isError: receiptFailed,
    isFetching: receiptFetching,
    refetch: retryReceipt,
  } = useQuery({
    queryKey: ["library", "receipt", asset.request_id, everyone],
    enabled: Boolean(asset.request_id),
    queryFn: () =>
      mediaRequest<ReceiptCost>(
        `/me/api/receipts/${encodeURIComponent(asset.request_id!)}${everyone ? "?scope=everyone" : ""}`,
      ),
    retry: false,
  });
  const prompt = asset.prompt?.trim() ?? "";
  // The maker of an item with no prompt learns why, when their settings say.
  const viewer = useViewer();
  const {
    data: promptMemory,
    isError: promptMemoryError,
    isFetching: promptMemoryFetching,
    refetch: refetchPromptMemory,
  } = useQuery({
    queryKey: promptMemoryKey,
    queryFn: accountApi.promptMemory,
    enabled: maker && !prompt,
    retry: false,
  });
  useEffect(() => {
    if (viewer.id) markCreationOpened(viewer.id, asset.asset_id);
  }, [viewer.id, asset.asset_id]);
  const notSavedReason = promptNotSavedReason(
    tryKeepState(promptMemory?.prompt_memory),
    Boolean(viewer.admin),
  );
  // Only where the browser has a share sheet (phones, mostly).
  const [canShare, setCanShare] = useState(false);
  useEffect(() => setCanShare(typeof navigator.share === "function"), []);
  const created = toDateValue(asset.created_at);
  const recordedModel = receipt?.resolved_model || receipt?.requested_model || asset.model;
  const model = recordedModel ? nameModel(recordedModel, receipt?.resolved_workflow) : "";
  const title = shortTitle(asset);
  // The gateway offers the file with details only to its maker, while the
  // prompt is kept.
  const fileAvailable = !!renewed?.download_url && !renewalFailed;
  const detailsDownload =
    Boolean(asset.workflow_download_url) && (fileAvailable || asset.available !== false);
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
                    ? "bg-card text-card-foreground border-border rounded-panel max-w-lg flex-col gap-6 border px-8 py-8"
                    : "h-full",
                )}
              >
                {asset.kind === "audio" ? (
                  <>
                    {/* The clip on one neutral card (D1.5): its kind and the
                        player. Its words are in the details beside it, once,
                        as a picture's are (D1.5b: the prompt showed twice). */}
                    <p className="text-muted-foreground text-badge relative flex w-full items-center gap-1.5">
                      <AudioLines aria-hidden="true" className="size-3.5" />
                      Audio
                    </p>
                  </>
                ) : null}
                {renewing ? <p role="status">Opening your file…</p> : renewalFailed ? (
                  <p role="status">The gateway couldn’t prepare this file. <button className="text-link min-h-11 px-3 underline" onClick={() => void renew()}>Try again</button></p>
                ) : renewed ? <Preview
                  key={`${asset.asset_id}-${renewed.download_url}`}
                  asset={{ ...asset, download_url: renewed.download_url, available: true, unavailable_reason: null }}
                  large
                  everyone={everyone}
                /> : null}
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
              {detailsDownload ? (
                <p className="text-muted-foreground text-meta">
                  Download with details includes prompt and workflow text.
                  Downloaded copies can’t be recalled.
                </p>
              ) : null}
              <div className="flex items-start justify-between gap-3">
                <DialogPrimitive.Title className="text-verdict min-w-0 text-balance break-words">
                  {title}
                </DialogPrimitive.Title>
                <div className="-mt-1 -mr-1.5 flex shrink-0 items-center gap-1">
                  {onDelete || detailsDownload ? (
                    // The row menu of the list template: a 32 icon button.
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Actions for ${title}`}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {detailsDownload ? (
                          // The maker's own copy with the prompt and settings
                          // inside the file, as Photos' "Export Unmodified
                          // Original" sits apart from Share.
                          <DropdownMenuItem
                            disabled={downloading}
                            onSelect={() => onDownload(asset, true)}
                          >
                            <FileDown aria-hidden />
                            {downloading ? "Preparing…" : "Download with details"}
                          </DropdownMenuItem>
                        ) : null}
                        {onDelete ? (
                          <>
                            {detailsDownload ? <DropdownMenuSeparator /> : null}
                            <DropdownMenuItem
                              variant="destructive"
                              onSelect={() => onDelete(asset)}
                            >
                              <Trash2 aria-hidden />
                              Delete
                            </DropdownMenuItem>
                          </>
                        ) : null}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  ) : null}
                  <DialogPrimitive.Close
                    className="text-muted-foreground hover:bg-muted hover:text-foreground rounded-control hidden size-8 items-center justify-center md:flex"
                    aria-label="Close"
                  >
                    <X aria-hidden="true" className="size-4" />
                  </DialogPrimitive.Close>
                </div>
              </div>
              <p className="text-meta flex flex-wrap items-center gap-x-2 gap-y-1">
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
              {asset.available === false && !fileAvailable ? (
                <p className="bg-muted rounded-control text-meta p-3">
                  {confirmedGone(asset.unavailable_reason)
                    ? "The gateway no longer stores this file."
                    : "This file is unavailable."}
                  {remains ? ` ${remains}` : ""}
                </p>
              ) : null}
              {/* Share (the file itself) is the primary at 40 where the
                  browser has a share sheet; every other action is 32. */}
              <div className="flex flex-wrap items-center gap-2">
                {(fileAvailable || asset.available !== false) && canShare ? (
                  <Button
                    type="button"
                    size="md"
                    disabled={downloading}
                    aria-busy={downloading || undefined}
                    onClick={() => onShare(asset)}
                  >
                    <Share aria-hidden="true" />
                    {downloading ? "Preparing…" : "Share"}
                  </Button>
                ) : null}
                {fileAvailable || asset.available !== false ? (
                  <Button
                    type="button"
                    size="sm"
                    variant={canShare ? "outline" : "default"}
                    disabled={downloading}
                    aria-busy={downloading || undefined}
                    onClick={() => onDownload(asset)}
                  >
                    <Download aria-hidden="true" />
                    {downloading ? "Preparing…" : "Download"}
                  </Button>
                ) : null}
                {prompt ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      void navigator.clipboard.writeText(prompt).then(
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
                ) : null}
                {asset.request_id ? (
                  <Button asChild variant="outline" size="sm">
                    <a
                      href={`/usage/?receipt=${encodeURIComponent(asset.request_id)}`}
                      aria-label={`Open receipt ${asset.request_id}`}
                    >
                      <ReceiptText aria-hidden="true" />
                      Receipt
                    </a>
                  </Button>
                ) : null}
              </div>
              {receiptFailed ? <p role="status">The gateway couldn’t load the receipt. <Button variant="link" disabled={receiptFetching} onClick={() => void retryReceipt()}>{receiptFetching ? "Loading…" : "Retry receipt"}</Button></p> : null}
              {maker && !prompt ? <QueryFeedback label="prompt settings" query={{ isError: promptMemoryError, isFetching: promptMemoryFetching, refetch: refetchPromptMemory }} /> : null}
              <KeyValueList
                className="figures"
                items={presentFacts([
                  ["Model", model],
                  [
                    "Created",
                    created
                      ? created.toLocaleString(undefined, {
                          dateStyle: "medium",
                          timeStyle: "short",
                        })
                      : "",
                  ],
                  [
                    "Size",
                    asset.width && asset.height
                      ? `${asset.width} × ${asset.height}`
                      : "",
                  ],
                  [
                    "Length",
                    asset.duration_s != null &&
                    Number.isFinite(asset.duration_s)
                      ? formatDuration(asset.duration_s)
                      : "",
                  ],
                  [
                    "Made in",
                    asset.generation_ms == null
                      ? ""
                      : `${(asset.generation_ms / 1000).toFixed(1)} s`,
                  ],
                  ["Charge", charge],
                ])}
              />
              {prompt && prompt.replace(/[.!?]$/, "") !== title ? (
                <section>
                  <h3 className="text-muted-foreground text-meta mb-1">
                    Prompt
                  </h3>
                  <p className="text-body break-words">{prompt}</p>
                </section>
              ) : !prompt && maker ? (
                // The maker learns why Copy prompt is missing. Nobody else
                // is told a prompt ever existed.
                <p className="text-muted-foreground text-meta flex items-center gap-1.5">
                  <EyeOff aria-hidden="true" className="size-4 shrink-0" />
                  {notSavedReason
                    ? `No saved prompt. ${notSavedReason}`
                    : "No saved prompt"}
                </p>
              ) : null}
              <details className="group">
                <summary className="text-muted-foreground hover:text-foreground text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
                  Details
                </summary>
                <KeyValueList
                  className="mt-2"
                  items={presentFacts([
                    [
                      "Request receipt",
                      asset.request_id ? (
                        <a
                          className="text-link text-meta font-mono break-all underline-offset-4 hover:underline"
                          href={`/usage/?receipt=${encodeURIComponent(asset.request_id)}`}
                          aria-label={`View receipt ${asset.request_id}`}
                        >
                          {asset.request_id}
                        </a>
                      ) : (
                        "Not recorded"
                      ),
                    ],
                    [
                      "Model name",
                      recordedModel ? (
                        <span className="text-meta font-mono break-all">
                          {safeModelId(recordedModel)}
                        </span>
                      ) : (
                        ""
                      ),
                    ],
                    ["Seed", asset.seed ?? ""],
                    ["App", asset.source?.label ?? ""],
                    ["Created (UTC)", absolute(asset.created_at) ?? ""],
                    [
                      "File",
                      asset.available === false
                        ? (asset.unavailable_reason ?? "")
                        : "",
                    ],
                  ])}
                />
                <section className="mt-3">
                  <h3 className="text-muted-foreground text-meta mb-1">
                    Workflow
                  </h3>
                  <pre className="bg-muted rounded-control max-h-40 overflow-auto p-3 font-mono text-xs whitespace-pre-wrap">
                    {asset.workflow
                      ? typeof asset.workflow === "string"
                        ? asset.workflow
                        : JSON.stringify(asset.workflow, null, 2)
                      : "Not recorded"}
                  </pre>
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

export default function LibraryPage() {
  const viewer = useViewer();
  const searchParams = useSearchParams();
  const linkedJobId = searchParams?.get("job");
  const queryClient = useQueryClient();
  const {
    data: modelCatalog,
    isError: modelCatalogError,
    isFetching: modelCatalogFetching,
    refetch: refetchModelCatalog,
  } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  const preparing = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const removedAssetIds = useRef(new Set<string>());
  const deletingAssetId = useRef<string | null>(null);
  const canChooseOwner = ["owner", "superadmin"].includes(viewer.role);
  const [kind, setKind] = useState("all");
  const [model, setModel] = useState("all");
  const [date, setDate] = useState<DateFilter>("all");
  const [search, setSearch] = useState("");
  // On a phone the search field opens from a button in the control row.
  const [phoneSearch, setPhoneSearch] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [owner, setOwner] = useState("default");
  const [source, setSource] = useState("all");
  const [selected, setSelected] = useState<MediaAsset | null>(null);
  const [closedDeepLinkId, setClosedDeepLinkId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<MediaAsset | null>(null);
  const [downloadingAssetId, setDownloadingAssetId] = useState<string | null>(
    null,
  );
  const instanceView = canChooseOwner && owner !== "mine";
  const debouncedSearch = useDebouncedValue(search, 250);
  // The payload also supplies identity/apps, so keep its initial read. Only
  // poll rapidly when running work can actually appear under these filters.
  const showRunningMedia =
    ["default", "everyone", "mine"].includes(owner) &&
    model === "all" &&
    source === "all" &&
    !debouncedSearch.trim();
  const {
    data: home,
    isError: homeError,
    isFetching: homeFetching,
    refetch: refetchHome,
  } = useQuery({
    queryKey: ["account", "home"],
    queryFn: productApi.home,
    retry: false,
    refetchInterval: (query) =>
      showRunningMedia &&
      inProgressMedia(query.state.data?.receipts).some(
        (item) => kind === "all" || item.kind === kind,
      )
        ? 15_000
        : false,
  });
  const filters = { kind, model, date, search: debouncedSearch, owner, source };
  const activeFilters = hasActiveFilters(filters);
  const mediaPath = buildMediaPath(filters, canChooseOwner);
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
    setClosedDeepLinkId(null);
    setSelected(asset);
    const params = new URLSearchParams(window.location.search);
    params.set("asset", asset.asset_id);
    window.history.replaceState(null, "", `?${params.toString()}`);
  }
  function closeAsset() {
    setClosedDeepLinkId(selected?.asset_id ?? deepLinkedAssetId);
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
    error: libraryError,
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
  const { people, lookup: personFor, query: peopleQuery } = usePeople(canChooseOwner, sources);
  useEffect(() => {
    setSelected((current) => current ? assets.find((item) => item.asset_id === current.asset_id) ?? current : null);
  }, [assets]);
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
  // A removed deep link forgets which link was closed; adjusted during
  // render, not in an effect, so no stale commit shows between them.
  const [previousDeepLinkId, setPreviousDeepLinkId] = useState(deepLinkedAssetId);
  if (previousDeepLinkId !== deepLinkedAssetId) {
    setPreviousDeepLinkId(deepLinkedAssetId);
    if (!deepLinkedAssetId) setClosedDeepLinkId(null);
  }
  useEffect(() => {
    if (!deepLinkedAssetId) return;
    if (closedDeepLinkId === deepLinkedAssetId) return;
    if (selected?.asset_id === deepLinkedAssetId) return;
    if (listedDeepLinkedAsset) {
      setSelected(listedDeepLinkedAsset);
      return;
    }
    if (deepLinkedAsset?.asset && isMediaKind(deepLinkedAsset.asset.kind)) {
      setSelected(deepLinkedAsset.asset);
    }
  }, [
    deepLinkedAsset,
    deepLinkedAssetId,
    listedDeepLinkedAsset,
    selected?.asset_id,
    closedDeepLinkId,
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
      showRunningMedia
        ? runningMedia.filter((item) => kind === "all" || item.kind === kind)
        : [],
    [showRunningMedia, kind, runningMedia],
  );
  // When running work finishes, its creation is now in the list.
  const runningCount = useRef(runningMedia.length);
  useEffect(() => {
    if (runningMedia.length < runningCount.current) void refetch();
    runningCount.current = runningMedia.length;
  }, [refetch, runningMedia.length]);
  const goneConfirmed = goneAssets.every((asset) =>
    confirmedGone(asset.unavailable_reason),
  );
  const [goneOpen, setGoneOpen] = useState(false);
  const visiblePeople = instanceView
    ? Array.from(
        new Set(
          [...storedAssets, ...(goneOpen ? goneAssets : [])]
            .map(personFor)
            .filter((person): person is string => Boolean(person)),
        ),
      )
    : [];
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
  const nameModel: NameModel = (id, workflowId) =>
    modelDisplayName(id, modelCatalog, nameCapabilities, workflowId);
  // Delete only what the viewer made. In a person or "Everyone" view that must
  // be shown by the creation's app; if it cannot be, Delete is not offered.
  const viewerUserId = home?.user?.id ?? null;
  const viewerAppIds = new Set(
    (Array.isArray(home?.apps) ? home.apps : []).map((app) => app.id),
  );
  /** The viewer made this item: they hold its prompt, or the gateway names
   * them as its maker. Owning the App is not enough (CORE_CONTRACTS). */
  function isMaker(asset: MediaAsset) {
    if (asset.prompt) return true;
    const me = viewerUserId ?? viewer.id ?? null;
    return Boolean(me && asset.source?.person_id === me);
  }
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
        "delete",
      );
    },
    onSuccess: (_result, asset) => {
      removedAssetIds.current.add(asset.asset_id);
      toast.success("Deleted from your library.");
      closeAsset();
      void queryClient.invalidateQueries({ queryKey: ["library"] });
    },
    onError: (error) => toast.error(failureCopy(error)),
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
            // A toolbar control: 32, like the segments and Filters (44 on a phone).
            size="sm"
            className={place === "sheet" ? "w-full" : "w-auto min-w-40"}
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
  const days = dayMarks(
    storedAssets.map((asset) => toDateValue(asset.created_at)),
    new Date(),
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
          of controls filled half the screen). On a phone the row is the kind
          switch, a search button and Filters, so the controls stay under a
          fifth of the screen (D1.5); the search field opens below it. The
          heading keeps a ref so the viewer can return focus to it. */}
      {linkedJobId ? <JobResult jobId={linkedJobId} /> : null}
      <header className="mb-(--space-title) flex min-h-8 items-center">
        <h1 ref={headingRef} tabIndex={-1} className="text-title">
          Library
          {hasLoadedAssets && !hasNextPage && !activeFilters ? (
            // The count is what the grid shows; files no longer stored are
            // counted beside it, not inside it (red team round 2).
            <span className="text-subtle-foreground figures text-heading ml-2.5 font-normal">
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
            className="text-link text-meta ml-3 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
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
      {/* The toolbar and the grid it filters are one block, 24 apart. */}
      <div>
        <section
          aria-label="Library filters"
          className="mb-6 flex flex-wrap items-center gap-2"
        >
          <label
            className={cn(
              "relative block min-w-0 sm:w-64",
              phoneSearch || search
                ? "max-sm:order-last max-sm:w-full"
                : "max-sm:hidden",
            )}
          >
            <span className="sr-only">Search your creations</span>
            <Search
              className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              aria-hidden
            />
            <Input
              ref={searchRef}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search your creations"
              size="sm"
              className="pl-9"
            />
          </label>
          {/* The kind switch stays one tap away on a phone too (D1.4: audio
            was buried behind Filters). */}
          <Segmented
            label="Kind"
            className="max-sm:min-w-0 max-sm:flex-1"
            value={kind}
            onChange={setKind}
            options={[
              { value: "all", label: "All" },
              { value: "image", label: "Images", icon: ImageIcon },
              { value: "video", label: "Video", icon: Video },
              { value: "audio", label: "Audio", icon: AudioLines },
            ]}
          />
          <Button
            type="button"
            variant="outline"
            size="icon-lg"
            aria-label="Search"
            aria-expanded={phoneSearch || Boolean(search)}
            onClick={() => {
              const open = !phoneSearch;
              setPhoneSearch(open);
              if (open) requestAnimationFrame(() => searchRef.current?.focus());
            }}
            className="sm:hidden"
          >
            <Search aria-hidden="true" />
          </Button>
          <div className="contents max-sm:hidden">{personPicker("row")}</div>
          {visiblePeople.length > 1 ? (
            <div
              role="group"
              aria-label="Creation makers"
              className="text-muted-foreground text-meta flex min-w-0 items-center gap-3 overflow-x-auto whitespace-nowrap max-sm:order-last max-sm:w-full max-sm:flex-wrap max-sm:gap-x-3 max-sm:gap-y-1 max-sm:overflow-visible max-sm:whitespace-normal"
            >
              {visiblePeople.map((person) => (
                <span key={person} className="inline-flex min-w-0 items-center gap-1.5">
                  <PersonMark name={person} size="2xs" label={person} />
                  <span className="min-w-0 break-words">{person}</span>
                </span>
              ))}
            </div>
          ) : null}
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
        <QueryFeedback label="model names" query={{ isError: modelCatalogError, isFetching: modelCatalogFetching, refetch: refetchModelCatalog }} />
        {canChooseOwner ? <QueryFeedback label="people" query={peopleQuery} /> : null}
        {showRunningMedia ? <QueryFeedback label="running work" query={{ isError: homeError, isFetching: homeFetching, refetch: refetchHome }} /> : null}
        {deepLinkedAssetId && closedDeepLinkId !== deepLinkedAssetId && !selected && deepLinkedAssetFetching ? <p role="status">Opening that creation…</p> : null}
        {deepLinkedAssetId && closedDeepLinkId !== deepLinkedAssetId && !selected && deepLinkedAssetError ? (
          <div className="mb-4">
            <ErrorState
              heading="That creation is unavailable"
              message="The gateway couldn’t open this creation. Try again or return to your library."
              onRetry={() => void refetchDeepLinkedAsset()}
              retrying={deepLinkedAssetFetching}
            />
            <Button variant="outline" className="mt-2" onClick={closeAsset}>
              Back to library
            </Button>
          </div>
        ) : null}
        {isLoading ? (
          <div
            role="status"
            aria-label="Loading library"
            aria-busy="true"
            className="space-y-3"
          >
            {/* The grid's own shape: three to six columns (D1.5: two big
              phone squares jumped to three). */}
            <div className={cn("grid grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6", mediaGridGap)}>
              {Array.from({ length: 12 }).map((_, index) => (
                <Skeleton key={index} className="rounded-media aspect-square" />
              ))}
            </div>
          </div>
        ) : null}
        {showInitialError ? (
          <ErrorState
            heading="Couldn’t load your library"
            message={failureCopy(libraryError)}
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
                  <Link href="/try/?capability=image">Make an image</Link>
                </Button>
              )
            }
          />
        ) : null}
        {!isLoading &&
        !showInitialError &&
        (storedAssets.length > 0 || inProgress.length > 0) ? (
          <div aria-busy={updating}>
            {/* One continuous grid, newest first (D1.6: date groups left
              part-filled rows); the day rides on its first tile. */}
            <div className={cn("grid grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6", mediaGridGap)}>
              {inProgress.map((item) => (
                <MakingTile key={item.id} kind={item.kind} startedAt={item.startedAt} />
              ))}
              {storedAssets.map((asset, index) => (
                <AssetTile
                  key={asset.asset_id}
                  asset={asset}
                  onOpen={openAsset}
                  everyone={instanceView}
                  person={instanceView ? personFor(asset) : null}
                  selected={selected?.asset_id === asset.asset_id}
                  day={days[index]}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
      {!isLoading && !showInitialError && goneAssets.length > 0 ? (
        <section
          id="gone-files"
          aria-label={
            goneConfirmed ? "Files no longer stored" : "Unavailable files"
          }
          className={cn(storedAssets.length > 0 && "mt-8 border-t pt-6")}
        >
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            <p className="text-meta" role="status">
              {goneAssets.length === 1
                ? goneConfirmed
                  ? "1 file is no longer stored."
                  : "1 file is unavailable."
                : goneConfirmed
                  ? `${goneAssets.length} files are no longer stored.`
                  : `${goneAssets.length} files are unavailable.`}{" "}
              {/* One explanation for owners and members alike. */}
              <span className="text-muted-foreground">Receipts remain.</span>
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
            <summary className="text-link text-meta flex min-h-8 w-fit cursor-pointer items-center font-medium max-sm:min-h-11">
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
        <p role="status" className="text-muted-foreground text-meta mt-3">
          Updating
        </p>
      ) : null}
      {showRetainedError ? (
        <div
          role="alert"
          className="border-destructive/30 bg-danger-tint text-destructive rounded-panel text-meta mt-6 flex flex-wrap items-center justify-center gap-3 border p-3"
        >
          <span>
            {isFetchNextPageError
              ? "The gateway couldn’t load more library items. Try again."
              : "The gateway couldn’t refresh your library. Try again."}
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
          downloading={downloadingAssetId !== null}
          maker={isMaker(selected)}
          onShare={(asset) => {
            if (preparing.current) return;
            preparing.current = true;
            setDownloadingAssetId(asset.asset_id);
            void shareCreation(asset, instanceView).catch((error) => {
              if (!(error instanceof Error && error.name === "AbortError")) toast.error(failureCopy(error));
            }).finally(() => {
              preparing.current = false;
              setDownloadingAssetId(null);
            });
          }}
          onDownload={(asset, withDetails) => {
            if (preparing.current) return;
            preparing.current = true;
            setDownloadingAssetId(asset.asset_id);
            void prepareDownload(asset, instanceView, withDetails).then(
              () => {
                preparing.current = false;
                toast.success("Download ready.");
                setDownloadingAssetId(null);
              },
              (error) => {
                preparing.current = false;
                toast.error(failureCopy(error));
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
        description="Removes this creation from your Library. Downloaded copies stay."
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
