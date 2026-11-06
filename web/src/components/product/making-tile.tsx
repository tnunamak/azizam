import { AudioLines, ImageIcon, Video } from "lucide-react";
import { cn } from "@/lib/cn";
import { RelativeTime } from "@/components/product/relative-time";

export type MakingKind = "image" | "video" | "audio";

export const MAKING_COPY: Record<MakingKind, string> = {
  image: "Making your picture",
  video: "Making your video",
  audio: "Making your audio",
};

const ICONS = { image: ImageIcon, video: Video, audio: AudioLines } as const;

/**
 * Work the gateway accepted and is still making: calm, not clickable. The
 * words stay visible at every width, because a bare icon on a phone does not
 * say what is happening or that it may not finish. `risk` is the one warning
 * the viewer needs; the start time is secondary and may be dropped on a phone.
 */
export function MakingTile({
  kind,
  startedAt,
  risk,
  className,
}: {
  kind: MakingKind;
  /** Epoch seconds. */
  startedAt?: number | null;
  risk?: string | null;
  className?: string;
}) {
  const Icon = ICONS[kind];
  return (
    <div
      className={cn(
        "bg-muted text-muted-foreground rounded-media flex aspect-square min-w-0 flex-col items-center justify-center gap-1 overflow-hidden p-2 text-center sm:gap-2 sm:p-3",
        className,
      )}
    >
      <Icon
        aria-hidden="true"
        className={cn(
          "size-5 shrink-0 motion-safe:animate-pulse",
          // A phone tile is about 86 px wide: with a warning the words need the room.
          risk && "max-sm:hidden",
        )}
      />
      <p className="text-foreground text-badge sm:text-meta font-medium">
        {MAKING_COPY[kind]}
      </p>
      {startedAt != null ? (
        <p className="text-badge font-normal max-sm:sr-only">
          Started <RelativeTime ts={startedAt} fallback="" />
        </p>
      ) : null}
      {risk ? (
        <p className="text-warning text-badge">
          <span className="sm:hidden">May not finish</span>
          <span className="max-sm:sr-only">{risk}</span>
        </p>
      ) : null}
    </div>
  );
}
