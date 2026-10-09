"use client";

import type { ReactNode } from "react";
import { AudioLines, Music2, Play, Waves } from "lucide-react";
import { cn } from "@/lib/cn";
import { useAudioPeaks } from "@/lib/audio-peaks";

/** What kind of sound a model makes, in the glossary's words. */
export function audioKind(model?: string | null): {
  label: "Voice" | "Music" | "Sound effect" | "Audio";
  icon: typeof AudioLines;
} {
  const value = String(model ?? "").toLowerCase();
  if (/tts|speech|voice|kokoro|chatterbox|supertonic|omnivoice/.test(value))
    return { label: "Voice", icon: AudioLines };
  if (/music|song|ace-?step|musicgen/.test(value))
    return { label: "Music", icon: Music2 };
  if (/sfx|sound|effect|stable-audio|audioldm/.test(value))
    return { label: "Sound effect", icon: Waves };
  return { label: "Audio", icon: AudioLines };
}

/**
 * Bars from measured peaks. With `progress` (0–1) the played part is solid
 * and the rest faint, so the waveform doubles as the position.
 */
export function Waveform({
  peaks,
  progress,
  className,
}: {
  peaks: number[] | null;
  progress?: number;
  className?: string;
}) {
  if (!peaks)
    return (
      <span
        aria-hidden="true"
        className={cn("h-px flex-1 bg-current/25", className)}
      />
    );
  return (
    <svg
      aria-hidden="true"
      viewBox={`0 0 ${peaks.length * 3} 24`}
      preserveAspectRatio="none"
      className={cn("h-6 min-w-0 flex-1", className)}
    >
      {peaks.map((peak, index) => {
        const height = Math.max(2, peak * 24);
        const played =
          progress === undefined
            ? true
            : (index + 0.5) / peaks.length <= progress;
        return (
          <rect
            key={index}
            x={index * 3}
            y={(24 - height) / 2}
            width={2}
            height={height}
            rx={1}
            className={cn("fill-current", played ? "opacity-90" : "opacity-40")}
          />
        );
      })}
    </svg>
  );
}

/** The duration badge's look: a dark scrim pill that reads over any picture. */
function mediaBadge(glyphOnly: boolean) {
  return cn(
    "bg-media-badge text-media-badge-foreground flex h-6 shrink-0 items-center gap-1 rounded-full text-xs font-medium [&_svg]:size-3",
    glyphOnly ? "w-6 justify-center" : "px-2",
  );
}

/**
 * The small badge on a media tile that says what it is and how long: a play
 * glyph and "0:03" on video and audio (Apple Photos' duration badge). One
 * style for both kinds. Without a duration it is the glyph alone.
 */
export function MediaBadge({
  icon: Icon,
  children,
  className,
}: {
  icon: typeof AudioLines;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        mediaBadge(children == null),
        "pointer-events-none",
        className,
      )}
    >
      <Icon aria-hidden="true" />
      {children}
    </span>
  );
}

/**
 * The badge as a play button, for audio that plays in place. It keeps the
 * badge's look; an invisible margin around it makes a 40 px target.
 */
export function mediaBadgeButton(glyphOnly = false) {
  return cn(
    mediaBadge(glyphOnly),
    "hover:bg-media-badge-hover focus-visible:ring-ring relative z-20 cursor-pointer transition-colors duration-(--motion-fast) after:absolute after:-inset-2 after:content-[''] focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none",
  );
}

/**
 * An audio creation's tile face (D1.5): one calm neutral surface for every
 * clip, its real waveform (or the glyph when it cannot be measured) and the
 * duration badge, so audio keeps the grid's rhythm instead of being its
 * loudest tile. Who made it is the person filter's job; the words are in
 * the hover title and the viewer. `control`, when given, replaces the
 * badge (a play button that looks like it).
 */
export function AudioFace({
  peaksKey,
  src,
  visible,
  duration,
  control,
  status,
  className,
}: {
  peaksKey: string;
  src?: string | null;
  visible: boolean;
  duration?: string | null;
  control?: ReactNode;
  /** A short error line, shown at the top. */
  status?: ReactNode;
  className?: string;
}) {
  const peaks = useAudioPeaks(peaksKey, src, visible);
  return (
    <div
      className={cn(
        "bg-muted text-muted-foreground relative flex size-full items-center justify-center",
        className,
      )}
    >
      {peaks ? (
        <Waveform peaks={peaks} className="h-1/4 w-3/4 flex-none" />
      ) : (
        <AudioLines aria-hidden="true" className="size-8" />
      )}
      {status ? (
        <span className="text-foreground absolute inset-x-2 top-2 text-xs font-medium">
          {status}
        </span>
      ) : null}
      <span className="absolute right-2 bottom-2 z-20">
        {control ?? <MediaBadge icon={Play}>{duration ?? "Audio"}</MediaBadge>}
      </span>
    </div>
  );
}
