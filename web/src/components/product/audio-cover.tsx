"use client";

import type { ReactNode } from "react";
import { AudioLines, Music2, Waves } from "lucide-react";
import { cn } from "@/lib/cn";
import { useAudioPeaks } from "@/lib/audio-peaks";
import { personHueVar } from "./person-mark";

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

/**
 * The words a cover can show whole: every full sentence that fits in `max`
 * characters, else the longest run of whole words with an ellipsis. A cover
 * never stops mid-word, and stops mid-sentence only when the first sentence
 * alone is too long (D1.3: "voice cards truncate their text mid-sentence").
 */
export function coverWords(text: string, max: number): string {
  const clean = text.trim().replace(/\s+/g, " ");
  if (clean.length <= max) return clean;
  let out = "";
  for (const sentence of clean.match(/[^.!?]+[.!?]+(?:\s|$)/g) ?? []) {
    if ((out + sentence).trim().length > max) break;
    out += sentence;
  }
  if (out.trim()) return out.trim();
  // The last space at or before `max` ends the last whole word; one long
  // word with no space is cut at `max`.
  const space = clean.lastIndexOf(" ", max);
  const cut = space > 0 ? clean.slice(0, space) : clean.slice(0, max);
  // Prefer the end of a clause when one closes in the second half.
  const comma = cut.lastIndexOf(",");
  const words = comma >= max / 2 ? cut.slice(0, comma) : cut;
  // Never end on a joining word ("…and have a…"): drop them, then any
  // trailing punctuation.
  const trimmed = words
    .replace(
      /(?:\s+(?:a|an|the|and|or|but|to|of|in|on|at|by|for|with|from))+$/i,
      "",
    )
    .replace(/[,;:.!?]+$/, "");
  return `${trimmed || words}…`;
}

/**
 * The surface every audio cover is drawn on: deep water (the dark theme's
 * card, in both themes) washed with the maker's hue, so a clip carries the
 * same weight as a photograph beside it and says whose it is. `dark` scopes
 * the dark tokens to the cover, so text and controls inside stay legible.
 */
export function coverSurface(maker?: string | null): string {
  return cn(
    "dark bg-card text-foreground relative overflow-hidden",
    maker ? personHueVar(maker) : "[--person:var(--capability-voice)]",
  );
}

/**
 * The maker's wash; the first child of a cover surface. It is positioned
 * without a z-index, so the cover's content must be positioned too
 * (`relative`) to paint above it. The cover makes no stacking context: a
 * tile's play control (z-20) must stay above the tile's open button (z-10).
 */
export function CoverWash() {
  return (
    <span
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 bg-(--person)/24"
    />
  );
}

/**
 * An audio creation's cover, so a clip is as recognisable as a picture: its
 * maker's hue on deep water, what kind of sound it is, its words, its real
 * waveform and its length. The play control comes from the caller (a Button
 * with variant="cover").
 */
export function AudioCover({
  peaksKey,
  src,
  visible,
  maker,
  model,
  text,
  duration,
  status,
  control,
  compact = false,
  className,
}: {
  peaksKey: string;
  src?: string | null;
  visible: boolean;
  maker?: string | null;
  model?: string | null;
  text: string;
  duration?: string | null;
  /** A short error or state line, shown in place of the waveform. */
  status?: ReactNode;
  control: ReactNode;
  /**
   * Home's Recent tile: three lines of words; on a phone, where the tile is
   * a quarter of the width, only the maker's hue and the play disc.
   */
  compact?: boolean;
  className?: string;
}) {
  const peaks = useAudioPeaks(peaksKey, src, visible);
  const phoneHidden = compact ? "max-sm:hidden" : undefined;
  const kind = audioKind(model);
  const Icon = kind.icon;
  return (
    <div
      className={cn(
        coverSurface(maker),
        "rounded-media flex size-full flex-col gap-2 p-3",
        compact && "max-sm:items-center max-sm:justify-center max-sm:p-2",
        className,
      )}
    >
      <CoverWash />
      <p
        className={cn(
          "text-foreground/80 relative flex items-center justify-end gap-1 text-xs font-medium",
          phoneHidden,
        )}
      >
        <Icon aria-hidden="true" className="size-3.5 text-(--person)" />
        {kind.label}
      </p>
      <p
        className={cn(
          "relative min-h-0 flex-1 font-medium break-words",
          phoneHidden,
          compact
            ? "line-clamp-3 text-sm leading-snug"
            : "line-clamp-4 text-sm leading-snug",
        )}
      >
        {coverWords(text, compact ? 64 : 84)}
      </p>
      <div className="relative flex items-center gap-2">
        {control}
        {status ? (
          <span
            className={cn("min-w-0 flex-1 text-xs font-medium", phoneHidden)}
          >
            {status}
          </span>
        ) : (
          <Waveform
            peaks={peaks}
            className={cn("text-(--person)", phoneHidden)}
          />
        )}
        {duration && !status ? (
          <span
            className={cn(
              "figures text-foreground/80 shrink-0 text-xs font-medium",
              phoneHidden,
            )}
          >
            {duration}
          </span>
        ) : null}
      </div>
    </div>
  );
}
