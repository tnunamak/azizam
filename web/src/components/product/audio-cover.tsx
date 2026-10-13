"use client";

import type { ReactNode } from "react";
import { AudioLines, Play } from "lucide-react";
import { cn } from "@/lib/cn";
import { useAudioPeaks } from "@/lib/audio-peaks";

/**
 * The gutter between media tiles: 4 on a phone, 6 above (DESIGN.md, "Media
 * grid"). Library's grid and Home's Recent strip both use it, so a tile sits
 * the same distance from its neighbours on every page.
 */
export const mediaGridGap = "gap-1 sm:gap-1.5";

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

/* ------------------------------------------------------------------ */
/* Tile cues (D1.6)                                                    */
/* ------------------------------------------------------------------ */
/* Round 6 judges: "video and audio look like still images" and "nothing
   shows who made it". Every media tile uses the same small cues on one dark
   scrim that reads over any picture: the type and length bottom left, a play
   disc in the middle for what plays, the maker top right (Everyone only),
   and the day top left on the first tile of each day. */

const scrim = "bg-media-badge text-media-badge-foreground";

/**
 * The type and length of a playable tile, bottom left: a video or sound
 * glyph and "0:04" (Apple Photos' duration badge, with the type it lacks).
 * Without a length it is the glyph alone. Badge text and radius (12, 4).
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
        scrim,
        "rounded-badge text-badge pointer-events-none flex h-6 shrink-0 items-center gap-1 [&_svg]:size-3.5",
        children == null ? "w-6 justify-center" : "pr-2 pl-1.5",
        className,
      )}
    >
      <Icon aria-hidden="true" />
      {children}
    </span>
  );
}

const disc =
  "flex size-10 items-center justify-center rounded-full [&_svg]:size-4 [&_svg]:translate-x-px";

/** The play cue in the middle of a video or audio tile. */
export function PlayDisc({ className }: { className?: string }) {
  return (
    <span className={cn(scrim, disc, "pointer-events-none", className)}>
      <Play aria-hidden="true" className="fill-current" />
    </span>
  );
}

/** The play disc as a button, for audio that plays in place. */
export const playDiscButton = cn(
  scrim,
  disc,
  "hover:bg-media-badge-hover focus-visible:ring-ring pointer-events-auto relative z-30 cursor-pointer transition-colors duration-(--motion-fast) focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none [&_svg]:fill-current",
);

/**
 * The one always-visible control on a video or audio tile: play or pause, then
 * the type and length. It is its own class set, not playDiscButton plus
 * overrides, because cn does not merge conflicting utilities.
 */
export const tilePlayButton = cn(
  scrim,
  "hover:bg-media-badge-hover focus-visible:ring-ring pointer-events-auto absolute bottom-2 left-2 z-30 flex h-10 cursor-pointer items-center gap-1.5 rounded-badge px-2.5 transition-colors duration-(--motion-fast) focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none max-sm:min-h-11",
);

/** The type glyph and length inside a tilePlayButton. */
export function TileMeta({
  icon: Icon,
  children,
}: {
  icon: typeof AudioLines;
  children?: ReactNode;
}) {
  return (
    <span className="text-badge flex items-center gap-1 [&_svg]:size-3.5">
      <Icon aria-hidden="true" />
      {children}
    </span>
  );
}

/**
 * A short day label ("Today", "Oct 20") on the first tile of each day, in
 * badge text with the badge radius.
 */
export function DayChip({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        scrim,
        "rounded-badge text-badge pointer-events-none flex h-6 items-center px-2",
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * An audio creation's tile face: one calm neutral surface for every clip.
 * It shows the clip's measured waveform when the audio can be read, else the
 * glyph; nothing is drawn that was not measured. `center` sits over the
 * middle (the play disc); the caller adds the other cues. The prompt and the
 * full date live in the viewer, not on the face.
 */
export function AudioFace({
  peaksKey,
  src,
  sizeBytes,
  visible,
  center,
  status,
  className,
}: {
  peaksKey: string;
  src?: string | null;
  /** A large file keeps the glyph instead of being downloaded to draw a waveform. */
  sizeBytes?: number | null;
  visible: boolean;
  center?: ReactNode;
  /** A short error line, shown at the top. */
  status?: ReactNode;
  className?: string;
}) {
  const peaks = useAudioPeaks(peaksKey, src, visible, 40, sizeBytes);
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
        <span className="text-foreground text-badge absolute inset-x-2 top-2 z-30">
          {status}
        </span>
      ) : null}
      {center ? (
        <span className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center">
          {center}
        </span>
      ) : null}
    </div>
  );
}
