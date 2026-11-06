"use client";

import { useEffect, useState, type Ref } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { kindIcon } from "@/lib/media-kind";
import { FOCUS_RING } from "./focus-ring";

/** A preview link for a chosen file, revoked when the file changes. */
export function useObjectUrl(file: File | null | undefined): string | null {
  const [objectUrl, setObjectUrl] = useState<{ file: File; url: string } | null>(null);
  useEffect(() => {
    if (!file || typeof URL.createObjectURL !== "function") return;
    const next = URL.createObjectURL(file);
    setObjectUrl({ file, url: next });
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return objectUrl && objectUrl.file === file ? objectUrl.url : null;
}

/**
 * What every media input shows once something is chosen: a thumbnail (or the kind's icon), the name (the Library's
 * title for an item taken from it, else the file's name), a line under it with where it came from and what and how
 * big it is, and Replace and remove controls in a group on the right. The sites differ only in the words, never in
 * the layout.
 *
 * On a phone the thumbnail is top-aligned with the name and the controls stay in their group on the right, so they
 * are never a line of the text; the source sits on its own line over the facts ("PNG · 19 KB"), so no separator is
 * stranded at the end of a line. From `sm` up the source and facts share one line, "From your Library · PNG · 19 KB".
 */
export function PickedSummary({
  name,
  source,
  facts,
  kind,
  previewUrl,
  previewAlt,
  retryLabel,
  invalid = false,
  replaceRef,
  onReplace,
  onRemove,
  className,
}: {
  name: string;
  /** Where it came from, when that is worth saying ("From your Library"). */
  source?: string | null;
  /** What it is and how big: the type and the size. */
  facts: readonly (string | null | undefined | false)[];
  /** The family the preview draws ("image", "video"); any other kind shows its icon. */
  kind: string | null;
  previewUrl: string | null;
  previewAlt: string;
  retryLabel: string;
  invalid?: boolean;
  /** The Replace control, so a screen can move focus to it after a choice is taken. */
  replaceRef?: Ref<HTMLButtonElement>;
  /** Takes another in one step: opens the way the first was chosen. Leave it out where there is no other to take. */
  onReplace?: () => void;
  onRemove: () => void;
  className?: string;
}) {
  // A new preview link starts clean: a failed one is not carried over to the next file.
  const [failure, setFailure] = useState<{ url: string | null; failed: boolean; attempt: number }>({
    url: previewUrl,
    failed: false,
    attempt: 0,
  });
  if (failure.url !== previewUrl) setFailure({ url: previewUrl, failed: false, attempt: 0 });
  const { failed, attempt } = failure;
  const drawable = previewUrl !== null && (kind === "image" || kind === "video");
  const Icon = kindIcon(kind ?? "file");
  const tail = facts.filter((part): part is string => Boolean(part)).join(" · ");
  return (
    <div data-slot="picked-summary" className={cn("flex items-start gap-3 sm:items-center", className)}>
      <div
        className={cn(
          "bg-muted/40 rounded-media flex size-16 shrink-0 items-center justify-center overflow-hidden border",
          invalid ? "border-destructive" : "border-input",
        )}
      >
        {drawable && !failed ? (
          kind === "video" ? (
            <video
              key={attempt}
              src={previewUrl}
              muted
              preload="metadata"
              className="size-full object-cover"
              aria-label={previewAlt}
              onError={() => setFailure((current) => ({ ...current, failed: true }))}
            />
          ) : (
            // A local or signed address of the person's own picture: next/image has nothing to optimise.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={attempt}
              src={previewUrl}
              alt={previewAlt}
              className="size-full object-cover"
              onError={() => setFailure((current) => ({ ...current, failed: true }))}
            />
          )
        ) : (
          <Icon aria-hidden="true" className="text-muted-foreground size-6" />
        )}
      </div>
      <p className="min-w-0 flex-1">
        <span className="line-clamp-2 font-medium break-words" title={name}>
          {name}
        </span>
        {(source || tail) && (
          <span className="text-muted-foreground text-meta block">
            {source && <span className="max-sm:block">{source}</span>}
            {source && tail ? <span className="max-sm:hidden"> · </span> : null}
            {tail && <span className="whitespace-nowrap">{tail}</span>}
          </span>
        )}
        {drawable && failed && (
          <span role="status" className="text-muted-foreground text-meta block">
            Preview unavailable
          </span>
        )}
      </p>
      <div className="flex shrink-0 items-start gap-1 sm:items-center">
        {drawable && failed && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className={cn("max-sm:h-11", FOCUS_RING)}
            aria-label={retryLabel}
            onClick={() => setFailure((current) => ({ ...current, failed: false, attempt: current.attempt + 1 }))}
          >
            Retry
          </Button>
        )}
        {onReplace && (
          <button
            ref={replaceRef}
            type="button"
            onClick={onReplace}
            aria-label={`Replace ${name}`}
            className={cn(
              "text-muted-foreground hover:bg-muted hover:text-foreground text-meta inline-flex min-h-10 shrink-0 cursor-pointer items-center justify-center rounded-md px-3 font-medium max-sm:min-h-11",
              FOCUS_RING,
            )}
          >
            Replace
          </button>
        )}
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${name}`}
          title={`Remove ${name}`}
          className={cn(
            "text-muted-foreground hover:bg-muted hover:text-foreground inline-flex size-10 shrink-0 cursor-pointer items-center justify-center rounded-full max-sm:size-11",
            FOCUS_RING,
          )}
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>
    </div>
  );
}
