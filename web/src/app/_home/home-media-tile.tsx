"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AudioLines, ImageIcon, Pause, Play, Video } from "lucide-react";
import { cn } from "@/lib/cn";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/product/relative-time";
import { AudioFace, MediaBadge, playDiscButton } from "@/components/product/audio-cover";
import { homeApi, type HomeCreation } from "./home-api";
import { parseEpochSeconds } from "./home-model";

/** Shown while the tile is near the viewport: a video or an audio clip starts loading only then. */
function useHomePreviewVisible() {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
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

function firstWords(value?: string | null): string | null {
  const words = value?.trim().split(/\s+/).filter(Boolean) ?? [];
  if (!words.length) return null;
  return words.slice(0, 12).join(" ");
}

export function mediaKindLabel(kind: HomeCreation["kind"]): string {
  return kind === "video" ? "Video" : kind === "audio" ? "Audio" : "Image";
}

/** The picture, poster or clip of one creation; falls back to a labelled placeholder. */
function CreationMedia({
  creation,
  title,
  visible,
}: {
  creation: HomeCreation;
  title: string;
  visible: boolean;
}) {
  const Icon = creation.kind === "video" ? Video : creation.kind === "audio" ? AudioLines : ImageIcon;
  const { data: poster, isPending: posterQueryPending } = useQuery({
    queryKey: ["library", "poster", creation.poster_asset_id, false],
    enabled: creation.kind === "video" && !!creation.poster_asset_id,
    queryFn: () => homeApi.mediaDownload(creation.poster_asset_id!),
    retry: false,
  });
  const posterUrl = poster?.download_url;
  const [imageFailed, setImageFailed] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);
  const posterPending =
    creation.kind === "video" && !!creation.poster_asset_id && posterQueryPending && !posterFailed;
  const [videoFailed, setVideoFailed] = useState(false);
  const [audioError, setAudioError] = useState<"media" | "playback" | "unavailable" | null>(null);
  const [audioSrc, setAudioSrc] = useState(creation.download_url ?? undefined);
  const [isPlaying, setIsPlaying] = useState(false);
  const [audioLoading, setAudioLoading] = useState(false);
  const audioLoadLock = useRef(false);
  const audioRef = useRef<HTMLAudioElement>(null);
  const tileMounted = useRef(true);
  useEffect(() => {
    tileMounted.current = true;
    return () => {
      tileMounted.current = false;
    };
  }, []);

  const resetKey = `${creation.asset_id}:${creation.download_url ?? ""}:${posterUrl ?? ""}`;
  const [previousResetKey, setPreviousResetKey] = useState(resetKey);
  if (previousResetKey !== resetKey) {
    setPreviousResetKey(resetKey);
    setImageFailed(false);
    setPosterFailed(false);
    setVideoFailed(false);
    setAudioError(null);
    setIsPlaying(false);
    setAudioSrc(creation.download_url ?? undefined);
  }

  if (creation.kind === "image" && creation.download_url && !imageFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={creation.download_url}
        alt={title}
        loading="lazy"
        onError={() => setImageFailed(true)}
        className="size-full object-cover"
      />
    );
  }

  if (posterPending) {
    return (
      <div role="status" aria-label="Loading video poster" aria-busy="true" className="size-full">
        <Skeleton className="size-full rounded-none motion-reduce:animate-none" />
      </div>
    );
  }

  if (creation.kind === "video" && posterUrl && !posterFailed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={posterUrl}
        alt={title}
        loading="lazy"
        onError={() => setPosterFailed(true)}
        className="size-full object-cover"
      />
    );
  }

  if (creation.kind === "video" && creation.download_url && !videoFailed && !posterPending) {
    return (
      <video
        src={visible ? creation.download_url : undefined}
        preload="none"
        autoPlay={visible}
        muted
        playsInline
        onLoadedData={(event) => event.currentTarget.pause()}
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (Number.isFinite(video.duration) && video.duration > 0) {
            video.currentTime = Math.min(0.1, video.duration / 2);
          }
        }}
        onError={() => setVideoFailed(true)}
        className="size-full object-cover"
        aria-label={`${title} video preview`}
      />
    );
  }

  if (creation.kind === "audio" && creation.download_url) {
    return (
      <AudioFace
        peaksKey={creation.asset_id}
        src={audioSrc}
        sizeBytes={creation.size_bytes}
        visible={visible}
        status={
          audioLoading ? (
            <span role="status">Loading audio…</span>
          ) : audioError ? (
            <span role="status" className="text-destructive">
              {audioError === "unavailable"
                ? "Audio isn't available. Open it in Library."
                : audioError === "media"
                  ? "Audio preview unavailable. Play to retry."
                  : "Playback didn't start. Try again."}
            </span>
          ) : null
        }
        center={
          <>
            <audio
              ref={audioRef}
              preload="none"
              src={audioSrc}
              onPlay={() => {
                setAudioError(null);
                setIsPlaying(true);
              }}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
              onError={() => {
                setIsPlaying(false);
                setAudioError("media");
              }}
              className="sr-only"
            />
            <button
              type="button"
              // On a phone the tile opens the clip in Library instead.
              className={cn(playDiscButton, "max-sm:hidden")}
              disabled={audioLoading || audioError === "unavailable"}
              aria-label={audioLoading ? "Loading audio…" : `${isPlaying ? "Pause" : "Play"} ${title}`}
              onClick={async () => {
                const audio = audioRef.current;
                if (!audio || audioLoadLock.current || audioError === "unavailable") return;
                if (isPlaying) audio.pause();
                else {
                  audioLoadLock.current = true;
                  setAudioLoading(true);
                  try {
                    if (audioError === "media") {
                      const renewed = await homeApi.mediaDownload(creation.asset_id);
                      // The tile left the page while the renewal was in flight.
                      if (!tileMounted.current) return;
                      if (!renewed.download_url?.trim()) {
                        setAudioError("unavailable");
                        return;
                      }
                      setAudioSrc(renewed.download_url);
                      audio.src = renewed.download_url;
                      audio.load();
                    }
                    await audio.play();
                    setAudioError(null);
                  } catch {
                    setIsPlaying(false);
                    setAudioError(audioError === "media" || audio.error ? "media" : "playback");
                  } finally {
                    audioLoadLock.current = false;
                    setAudioLoading(false);
                  }
                }
              }}
            >
              {isPlaying ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            </button>
          </>
        }
      />
    );
  }

  return (
    <div
      role="img"
      aria-label={`${mediaKindLabel(creation.kind)} preview unavailable`}
      className="bg-muted text-muted-foreground flex size-full flex-col items-center justify-center gap-2 p-3 text-center"
    >
      <Icon aria-hidden="true" className="size-6" />
      <p className="text-badge">Preview unavailable</p>
    </div>
  );
}

/**
 * One finished creation in Recent: the picture, poster or clip, then its title (the prompt, or the
 * kind) and "Kind · when". Opening it goes to Library's viewer for that asset.
 */
export function MediaTile({ creation, className }: { creation: HomeCreation; className?: string }) {
  const { ref: visibilityRef, visible } = useHomePreviewVisible();
  const label = mediaKindLabel(creation.kind);
  const prompt = firstWords(creation.prompt);
  const title = prompt ?? label;
  const at = parseEpochSeconds(creation.created_at);
  const href = `/library/?asset=${encodeURIComponent(creation.asset_id)}`;
  // A picture or video is one link, named by its caption (what is visible). Audio keeps its play control
  // apart, so its link covers the tile only and the caption stays text.
  const linkName = prompt ? `Open ${label} in Library: ${prompt}` : `Open ${label} in Library`;
  const captionText = (
    <span className="mt-2 block min-w-0">
      <span className="text-foreground text-meta block truncate font-medium">{title}</span>
      <span className="text-muted-foreground text-badge block truncate">
        {label}
        {at != null ? (
          <>
            {" · "}
            <RelativeTime ts={at} fallback="" />
          </>
        ) : null}
      </span>
    </span>
  );
  const tile = "bg-muted rounded-media relative block aspect-square min-w-0 overflow-hidden";
  const media = (
    <div ref={visibilityRef} className="size-full">
      <CreationMedia creation={creation} title={title} visible={visible} />
    </div>
  );
  if (creation.kind === "audio") {
    return (
      <li className={cn("min-w-0", className)} data-slot="recent-tile">
        <div className={tile}>
          {media}
          <MediaBadge icon={AudioLines} className="absolute bottom-2 left-2 z-10" />
          <Link prefetch={false} href={href} aria-label={linkName} className="rounded-media absolute inset-0" />
        </div>
        {captionText}
      </li>
    );
  }
  return (
    <li className={cn("min-w-0", className)} data-slot="recent-tile">
      <Link prefetch={false} href={href} className="group block min-w-0">
        <div className={tile}>
          {media}
          {creation.kind === "video" ? <MediaBadge icon={Video} className="absolute bottom-2 left-2" /> : null}
        </div>
        {captionText}
      </Link>
    </li>
  );
}
