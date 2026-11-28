"use client";

import { useEffect, useState, type MouseEvent, type Ref } from "react";
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
 * The name gets all the free width. Where the row is wide (a container of 26 rem or more) the Replace and remove
 * controls sit in a group on its right. Where it is narrow they would take half of it and cut the name to two words,
 * so they move under the facts, in the name's column, and the name keeps the full width for up to two lines.
 *
 * On a phone the thumbnail is top-aligned with the name, and the source sits on its own line over the facts
 * ("PNG · 19 KB"), so no separator is stranded at the end of a line. From `sm` up the source and facts share one
 * line, "From your Library · PNG · 19 KB".
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
  ignoreRemoveClick,
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
  /** True for a click that is no choice of the person's (the tail of a double-click on a Library tile): the remove control does nothing. Replace is never ignored. */
  ignoreRemoveClick?: (event: MouseEvent<HTMLElement>) => boolean;
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
  // The picture's pixel size appears only once its own preview has loaded.
  const [dimensions, setDimensions] = useState<{ url: string | null; width: number; height: number } | null>(null);
  const imageSize =
    kind === "image" && !failed && dimensions && dimensions.url === previewUrl
      ? `${dimensions.width}×${dimensions.height}`
      : null;
  const drawable = previewUrl !== null && (kind === "image" || kind === "video");
  const Icon = kindIcon(kind ?? "file");
  const tail = [facts[0], imageSize, ...facts.slice(1)].filter((part): part is string => Boolean(part)).join(" · ");
  return (
    <div data-slot="picked-summary" className={cn("@container", className)}>
      <div className="grid grid-cols-[4rem_minmax(0,1fr)] items-start gap-x-3 gap-y-1 @[26rem]:grid-cols-[4rem_minmax(0,1fr)_auto] @[26rem]:items-center @[26rem]:gap-y-0">
        <div
          className={cn(
            "bg-muted/40 rounded-media row-span-2 flex size-16 shrink-0 items-center justify-center overflow-hidden border @[26rem]:row-span-1",
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
                onLoad={({ currentTarget }) => {
                  const { naturalWidth: width, naturalHeight: height } = currentTarget;
                  setDimensions(width > 0 && height > 0 ? { url: previewUrl, width, height } : null);
                }}
                onError={() => {
                  setDimensions(null);
                  setFailure((current) => ({ ...current, failed: true }));
                }}
              />
            )
          ) : (
            <Icon aria-hidden="true" className="text-muted-foreground size-6" />
          )}
        </div>
        <p className="min-w-0">
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
        <div className="col-start-2 row-start-2 -ml-3 flex flex-wrap items-center gap-1 @[26rem]:col-start-3 @[26rem]:row-start-1 @[26rem]:ml-0 @[26rem]:shrink-0">
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
            onClick={(event) => {
              if (!ignoreRemoveClick?.(event)) onRemove();
            }}
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
    </div>
  );
}
