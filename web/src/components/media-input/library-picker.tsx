"use client";

import { useEffect, useRef, useState } from "react";
import { ImageIcon, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/product";
import { kindIcon, kindLayout } from "@/lib/media-kind";
import { networkFetch } from "@/lib/network-error";
import {
  fileFitsTypes,
  formatDuration,
  mediaNoun,
  withArticle,
  type MediaTypes,
} from "@/lib/media-pick";
import { libraryDate, libraryTitle, type LibraryAsset, type LibraryPick } from "@/lib/library-file";

type LibraryPage = { assets?: LibraryAsset[]; next_cursor?: string | null };

const PAGE_SIZE = 40;

/**
 * The member's Library as a dialog of tiles, filtered to one kind of media (and,
 * when the input takes only some types of that kind, to those types). It lists;
 * the caller decides what a pick does.
 */
export function LibraryPicker({
  open,
  onOpenChange,
  kind,
  noun: nounOverride,
  types,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: string;
  /** The screen's own word for the thing chosen ("recording"); defaults to the kind's noun. */
  noun?: string;
  types?: MediaTypes;
  onPick: (pick: LibraryPick) => void;
}) {
  const noun = nounOverride ?? mediaNoun(kind);
  const Icon = kindIcon(kind);
  // A picture or video is a grid of thumbnails; a kind with nothing to look at (audio) is a list of rows.
  const layout = kindLayout(kind);
  const [state, setState] = useState<"idle" | "loading" | "error" | "ready">(
    "idle",
  );
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [more, setMore] = useState<"idle" | "loading" | "error">("idle");
  const [unshown, setUnshown] = useState<Set<string>>(new Set());
  // The tiles have scrolled under the header: it shows an edge so they do not run into it.
  const [scrolled, setScrolled] = useState(false);
  const content = useRef<HTMLDivElement>(null);
  const [attempt, setAttempt] = useState(0);
  const [previousOpen, setPreviousOpen] = useState(open);
  // Each reload or reopen is a new list; a "Show more" answer for an older
  // one must not land in it.
  const generation = useRef(0);
  if (open !== previousOpen) {
    setPreviousOpen(open);
    if (open) {
      setState("loading");
      setMore("idle");
    } else {
      // The list's signed links are not kept once the dialog is closed.
      setState("idle");
      setAssets([]);
      setNextCursor(null);
      setUnshown(new Set());
      setScrolled(false);
    }
  }

  function pageUrl(cursor?: string | null) {
    const query = new URLSearchParams({
      kind,
      limit: String(PAGE_SIZE),
    });
    if (cursor) query.set("cursor", cursor);
    return `/me/api/media?${query}`;
  }

  function usable(page: LibraryPage) {
    return (page.assets ?? []).filter(
      (asset) =>
        asset.asset_id &&
        asset.available !== false &&
        (asset.kind ?? kind) === kind &&
        // A list item with no declared type is kept: the download is checked.
        (!asset.media_type || fileFitsTypes({ type: asset.media_type }, types)),
    );
  }

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const mine = ++generation.current;
    networkFetch(pageUrl(), {
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Library unavailable");
        const page = (await response.json()) as LibraryPage;
        if (generation.current !== mine) return;
        setAssets(usable(page));
        setNextCursor(page.next_cursor ?? null);
        setUnshown(new Set());
        setState("ready");
      })
      .catch(() => {
        if (!controller.signal.aborted) setState("error");
      });
    return () => {
      controller.abort();
      // A "Show more" answer still in flight belongs to a closed list.
      generation.current += 1;
    };
    // pageUrl and usable only read `kind` and `types`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, attempt, kind, types?.join(",")]);

  // The dialog opens with focus on itself, not on a control: a close button that takes focus first draws its ring
  // over the title. When the tiles arrive, focus moves to the first one.
  useEffect(() => {
    if (!open || state !== "ready") return;
    const dialog = content.current;
    if (dialog && document.activeElement === dialog) {
      dialog.querySelector<HTMLElement>("ul button")?.focus();
    }
  }, [open, state]);

  async function showMore() {
    if (!nextCursor) return;
    const mine = generation.current;
    setMore("loading");
    try {
      const response = await networkFetch(pageUrl(nextCursor), {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Library unavailable");
      const page = (await response.json()) as LibraryPage;
      if (generation.current !== mine) return;
      setAssets((current) => {
        const seen = new Set(current.map((asset) => asset.asset_id));
        return [...current, ...usable(page).filter((a) => !seen.has(a.asset_id))];
      });
      setNextCursor(page.next_cursor ?? null);
      setMore("idle");
    } catch {
      if (generation.current === mine) setMore("error");
    }
  }

  function markUnshown(assetId: string) {
    setUnshown((current) => new Set(current).add(assetId));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        ref={content}
        showCloseButton={false}
        tabIndex={-1}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          content.current?.focus();
        }}
        className="max-h-[85vh] grid-rows-[auto_minmax(0,1fr)] overflow-hidden sm:max-w-[720px]!"
      >
        {/* The header and its close button stay put; the tiles scroll under them. */}
        <div
          data-slot="library-picker-header"
          data-scrolled={scrolled || undefined}
          className={`flex flex-col gap-2 border-b pb-3 text-left transition-[border-color,box-shadow] ${
            scrolled ? "border-border shadow-[0_8px_8px_-8px_rgb(0_0_0/0.18)]" : "border-transparent"
          }`}
        >
          <div className="flex items-start gap-3">
            {/* text-pretty: the page's balanced headings would wrap this title early and leave the line half empty. */}
            <DialogTitle className="min-w-0 flex-1 leading-snug! text-pretty">
              Choose {withArticle(noun)} from your Library
            </DialogTitle>
            {/* As tall as the title's first line, so the button is centred on it and sits inside the dialog's padding. */}
            <div className="flex h-[1.4rem] shrink-0 items-center">
              <DialogClose
                data-slot="dialog-close"
                className="ring-offset-background focus:ring-ring text-muted-foreground hover:text-foreground flex size-8 items-center justify-center rounded-md transition-colors focus:ring-2 focus:ring-offset-2 focus:outline-hidden max-sm:size-11 [&_svg]:pointer-events-none"
              >
                <X aria-hidden="true" className="size-4" />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
          </div>
          <DialogDescription>Newest first.</DialogDescription>
        </div>
        <div
          data-slot="library-picker-body"
          onScroll={(event) => setScrolled(event.currentTarget.scrollTop > 0)}
          className="-m-1 flex min-h-0 flex-col gap-4 overflow-y-auto p-1"
        >
          {state === "loading" && (
            layout === "grid" ? (
              <div aria-busy="true" className="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-5">
                {Array.from({ length: 10 }, (_, index) => (
                  <Skeleton key={index} className="aspect-square w-full" />
                ))}
              </div>
            ) : (
              <div aria-busy="true" className="flex flex-col gap-2">
                {Array.from({ length: 5 }, (_, index) => (
                  <Skeleton key={index} className="h-14 w-full" />
                ))}
              </div>
            )
          )}
          {state === "error" && (
            <ErrorState
              heading="Could not open your Library"
              message={`Your ${noun}s could not be loaded.`}
              action={{
                label: "Try loading it again",
                onClick: () => setAttempt((count) => count + 1),
              }}
            />
          )}
          {state === "ready" && assets.length === 0 && (
            <p className="text-muted-foreground text-sm">
              No {noun}s in your Library yet. Make one, or choose a file.
            </p>
          )}
          {state === "ready" && assets.length > 0 && layout === "list" && (
            <ul className="flex flex-col gap-1">
              {assets.map((asset) => {
                const title = libraryTitle(asset, noun);
                const length =
                  typeof asset.duration_s === "number" && asset.duration_s > 0
                    ? formatDuration(asset.duration_s)
                    : null;
                const facts = [length, libraryDate(asset.created_at)].filter(Boolean).join(" · ");
                return (
                  <li key={asset.asset_id} className="min-w-0">
                    <button
                      type="button"
                      title={title}
                      aria-label={title}
                      onClick={() => onPick(asset)}
                      className="focus-visible:ring-ring/50 hover:bg-muted/60 rounded-media flex min-h-14 w-full items-center gap-3 p-2 text-left focus-visible:ring-[3px] focus-visible:outline-none"
                    >
                      <span className="rounded-media bg-muted/40 text-muted-foreground flex size-10 shrink-0 items-center justify-center border">
                        <Icon aria-hidden="true" className="size-5" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span title={title} className="line-clamp-2 text-sm font-medium break-words">
                          {title}
                        </span>
                        {facts && <span className="text-muted-foreground text-meta block">{facts}</span>}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {state === "ready" && assets.length > 0 && layout === "grid" && (
            <ul className="grid grid-cols-3 gap-x-2 gap-y-3 sm:grid-cols-4 lg:grid-cols-5">
              {assets.map((asset) => {
                const title = libraryTitle(asset, noun);
                // The list's own signed links show the item; the download route
                // answers with a link, not an image.
                const link =
                  (kind === "image" ? asset.thumbnail_url : null) ??
                  asset.download_url;
                const src = link && !unshown.has(asset.asset_id) ? link : null;
                const length =
                  typeof asset.duration_s === "number" && asset.duration_s > 0
                    ? formatDuration(asset.duration_s)
                    : null;
                return (
                  <li key={asset.asset_id} className="min-w-0">
                    <button
                      type="button"
                      title={title}
                      aria-label={title}
                      onClick={() => onPick(asset)}
                      className="focus-visible:ring-ring/50 rounded-media flex w-full flex-col gap-1 text-left focus-visible:ring-[3px] focus-visible:outline-none"
                    >
                      <span className="rounded-media bg-muted/40 flex aspect-square w-full items-center justify-center overflow-hidden border">
                        {!src ? (
                          <span className="text-muted-foreground flex flex-col items-center gap-1 p-1 text-xs">
                            <ImageIcon aria-hidden="true" className="size-6" />
                            Preview unavailable
                          </span>
                        ) : kind === "video" ? (
                          <video
                            src={src}
                            muted
                            preload="metadata"
                            className="size-full object-cover"
                            onError={() => markUnshown(asset.asset_id)}
                          />
                        ) : (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            src={src}
                            alt=""
                            loading="lazy"
                            className="size-full object-cover"
                            onError={() => markUnshown(asset.asset_id)}
                          />
                        )}
                      </span>
                      <span title={title} className="text-meta line-clamp-2 font-medium break-words">
                        {title}
                      </span>
                      {length && (
                        <span className="text-muted-foreground text-meta -mt-1 block">{length}</span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {state === "ready" && more === "error" && (
            <p role="alert" className="text-destructive text-xs">
              Could not load more {noun}s. Try again.
            </p>
          )}
          {state === "ready" && nextCursor && (
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={more === "loading"}
              onClick={() => void showMore()}
            >
              {more === "loading" ? "Loading…" : "Show more"}
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
