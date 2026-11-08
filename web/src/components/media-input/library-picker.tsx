"use client";

import { type CSSProperties, useEffect, useId, useRef, useState } from "react";
import { Check, ImageIcon, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FOCUS_RING } from "./focus-ring";
import { noteLibraryTileClick } from "./use-media-pick";
import { cn } from "@/lib/cn";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState, ErrorState } from "@/components/product";
import { kindIcon, kindLayout } from "@/lib/media-kind";
import { networkFetch } from "@/lib/network-error";
import {
  fileFitsTypes,
  formatDuration,
  mediaNoun,
  withArticle,
  type MediaTypes,
} from "@/lib/media-pick";
import { groupLibraryByDay, libraryFullDate, libraryTitle, type LibraryAsset, type LibraryPick } from "@/lib/library-file";

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
  currentId,
  onPick,
  onChooseFile,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: string;
  /** The screen's own word for the thing chosen ("recording"); defaults to the kind's noun. */
  noun?: string;
  types?: MediaTypes;
  /** The item the screen already holds, when it came from the Library (Replace): marked, scrolled to and focused. */
  currentId?: string | null;
  onPick: (pick: LibraryPick) => void;
  /** Opens the screen's own file chooser: the empty Library's way forward. */
  onChooseFile?: () => void;
}) {
  const headingPrefix = useId();
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
  // More of the list lies below the fold: the dialog's bottom edge shows it, as its header's edge shows tiles above.
  const [moreBelow, setMoreBelow] = useState(false);
  // The days whose headings are pinned at the top of the scroll area, with their groups passing under them (two small
  // days side by side are pinned together).
  const [stuckDays, setStuckDays] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const content = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
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
      setMoreBelow(false);
      setQuery("");
    }
  }

  const needle = query.trim().toLowerCase();
  const shown = needle
    ? assets.filter((asset) => libraryTitle(asset, noun).toLowerCase().includes(needle))
    : assets;
  const now = new Date();
  const groups = groupLibraryByDay(shown, now);
  // A Library with no dates at all gets no heading: one "Undated" over everything says nothing.
  const dayHeadings = !(groups.length === 1 && groups[0].key === "undated");

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
  // over the title. When the tiles arrive, focus moves to the item the screen already holds (Replace), which is
  // scrolled into view, or else to the first one.
  useEffect(() => {
    if (!open || state !== "ready") return;
    const dialog = content.current;
    if (dialog && document.activeElement === dialog) {
      const items = Array.from(dialog.querySelectorAll<HTMLElement>("ul button"));
      const target = items.find((item) => item.getAttribute("aria-current") === "true") ?? items[0];
      target?.scrollIntoView?.({ block: "center" });
      target?.focus({ preventScroll: true });
    }
  }, [open, state]);

  // The bottom edge shows while more of the list lies below. Measured after the list changes, on a
  // scroll and when the window is resized.
  function measureBelow() {
    const body = bodyRef.current;
    if (!body) return;
    setMoreBelow(body.scrollTop + body.clientHeight < body.scrollHeight - 1);
  }
  // A day's heading is pinned while the scroll position is inside its section, past the section's top.
  function measureStuck() {
    const body = bodyRef.current;
    if (!body) return;
    const top = body.scrollTop;
    const days: string[] = [];
    for (const section of body.querySelectorAll<HTMLElement>("section[data-day]")) {
      if (section.offsetTop < top && section.offsetTop + section.offsetHeight > top) days.push(section.dataset.day ?? "");
    }
    setStuckDays((current) => (current.join() === days.join() ? current : days));
  }

  useEffect(() => {
    if (!open) return;
    measureBelow();
    window.addEventListener("resize", measureBelow);
    return () => window.removeEventListener("resize", measureBelow);
  });

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

  const noMatch = state === "ready" && assets.length > 0 && shown.length === 0;
  // An empty Library has no search to keep in place, so the phone sheet fits its content.
  const empty = state === "ready" && assets.length === 0;
  // The row or tile of the item the screen already holds: a check badge and `aria-current`, and for a tile a dark
  // teal border all round it, the way a photo app marks a selected photo.
  const isCurrent = (asset: LibraryAsset) => currentId != null && asset.asset_id === currentId;
  const currentMark = (
    <span
      data-slot="library-current-mark"
      aria-hidden="true"
      // The white ring keeps the dark disc apart from a dark picture under it; the disc stands out from a light one
      // on its own.
      className="bg-primary text-primary-foreground flex size-5 shrink-0 items-center justify-center rounded-full ring-2 ring-white"
    >
      <Check className="size-3.5" />
    </span>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        ref={content}
        showCloseButton={false}
        tabIndex={-1}
        aria-describedby={undefined}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          content.current?.focus();
        }}
        // From sm up a dialog whose top edge is fixed at 12 % of the window, so the search field does not move under
        // the person as the results shrink (a centred dialog re-centres with every change of height). Under 640 px a
        // bottom sheet that takes the screen's height, so the list has room and the header stays put above it; an empty
        // Library's sheet fits its content.
        className={cn(
          "max-h-[85vh] grid-rows-[auto_minmax(0,1fr)] overflow-hidden sm:top-[12vh] sm:max-h-[80vh] sm:max-w-[720px]! sm:translate-y-0 max-sm:top-auto max-sm:bottom-0 max-sm:left-0 max-sm:max-h-none max-sm:max-w-none max-sm:translate-x-0 max-sm:translate-y-0 max-sm:rounded-b-none",
          empty ? "max-sm:h-auto" : "max-sm:h-[calc(100dvh-1rem)]",
        )}
      >
        {/* The header and its close button stay put; the tiles scroll under them. */}
        <div
          data-slot="library-picker-header"
          data-scrolled={scrolled || undefined}
          className={`flex flex-col gap-3 border-b pb-3 text-left transition-[border-color,box-shadow] ${
            // A pinned day heading draws the edge itself, so the header does not draw a second one.
            scrolled && stuckDays.length === 0 ? "border-border shadow-[0_8px_8px_-8px_rgb(0_0_0/0.18)]" : "border-transparent"
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
                className={cn(
                  "text-muted-foreground hover:bg-border hover:text-foreground flex size-10 cursor-pointer items-center justify-center rounded-md transition-colors max-sm:size-11 [&_svg]:pointer-events-none",
                  FOCUS_RING,
                )}
              >
                <X aria-hidden="true" className="size-4" />
                <span className="sr-only">Close</span>
              </DialogClose>
            </div>
          </div>
          {state === "ready" && assets.length > 0 && (
            <div className="relative">
              <Search
                aria-hidden="true"
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                ref={searchField}
                type="search"
                size="sm"
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
                placeholder={`Search your ${noun}s`}
                aria-label={`Search your ${noun}s`}
                autoComplete="off"
                spellCheck={false}
                // The shared focus ring, not the field's lighter teal edge; the browser's own clear button (a blue x)
                // is hidden for ours.
                className={cn(
                  "focus-visible:border-input! pr-11 pl-9 [&::-webkit-search-cancel-button]:appearance-none",
                  FOCUS_RING,
                )}
              />
              {query && (
                <button
                  type="button"
                  aria-label="Clear search"
                  onClick={() => {
                    setQuery("");
                    searchField.current?.focus();
                  }}
                  className={cn(
                    "text-muted-foreground hover:bg-border hover:text-foreground absolute top-1/2 right-0 flex size-10 -translate-y-1/2 cursor-pointer items-center justify-center rounded-md transition-colors max-sm:size-11 [&_svg]:pointer-events-none",
                    FOCUS_RING,
                  )}
                >
                  <X aria-hidden="true" className="size-4" />
                </button>
              )}
            </div>
          )}
        </div>
        {/* The scroll area reaches 12 px into the dialog's padding: a list row's hover fill and every focus ring fit
            there, so rows and tiles can start on the title's left edge. */}
        <div className="relative -mx-3 -my-1 flex min-h-0 flex-col">
          <div
            data-slot="library-picker-body"
            ref={bodyRef}
            data-more-below={moreBelow || undefined}
            onScroll={(event) => {
              setScrolled(event.currentTarget.scrollTop > 0);
              measureBelow();
              measureStuck();
            }}
            className="peer relative flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-3 py-1"
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
            {empty && (
              <EmptyState
                flush
                icon={Icon}
                title={`No ${noun}s in your Library yet`}
                description="Make one, or choose a file from this device."
                action={
                  onChooseFile ? (
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => {
                        onOpenChange(false);
                        onChooseFile();
                      }}
                    >
                      Choose a file
                    </Button>
                  ) : undefined
                }
              />
            )}
            {noMatch && (
              <p role="status" className="text-muted-foreground text-sm">
                No {noun}s match “{query.trim()}”.
              </p>
            )}
            {state === "ready" && shown.length > 0 && (
              // Pictures: days with only a few items sit side by side on one row, each under its own heading, as
              // Google Photos lays out its days, so a day of two pictures does not leave most of a row empty. Every
              // tile keeps the one grid's width (--cols columns, 0.5 rem apart), and a day spans as many columns as it
              // has items, up to a full row.
              <div
                className={
                  layout === "grid"
                    ? "flex flex-wrap gap-x-2 gap-y-6 [--cols:3] sm:[--cols:4] lg:[--cols:5]"
                    : "flex flex-col gap-6"
                }
              >
                {groups.map((group, index) => {
                  const headingId = `${headingPrefix}-day-${index}`;
                  const span = `min(${group.items.length}, var(--cols))`;
                  const stuck = stuckDays.includes(group.key);
                  return (
                    <section
                      key={`${group.key}-${index}`}
                      data-day={dayHeadings ? group.key : undefined}
                      aria-labelledby={dayHeadings ? headingId : undefined}
                      // On a phone each day is its own full-width block, as in the Google Photos picker: three
                      // columns leave no room for a second day's heading beside the first.
                      className={cn("min-w-0", layout === "grid" && "max-sm:w-full!")}
                      // 0.1 px short, so two days that fill a row exactly never wrap on a rounding error.
                      style={
                        layout === "grid"
                          ? ({
                              "--span": span,
                              width: `calc((100% - (var(--cols) - 1) * 0.5rem) / var(--cols) * var(--span) + (var(--span) - 1) * 0.5rem - 0.1px)`,
                            } as CSSProperties)
                          : undefined
                      }
                    >
                      {dayHeadings && (
                        // A section label, as the Android photo picker sets "Today": a step under the dialog title, above
                        // the captions in size and weight, in ink. It stays at the top of the scroll
                        // area on the dialog's own surface while its group passes under it, with a hairline below it then.
                      // -top-1: sticky pins inside the area's 4 px top padding; this closes that gap so no tile shows above.
                        <h3
                          id={headingId}
                          data-stuck={stuck || undefined}
                          className={cn(
                            "text-foreground bg-background sticky -top-1 z-20 border-b pt-1 pb-2 text-base font-medium",
                            // Pinned: a hairline, and the tiles fade in under it rather than being cut flat (the fade
                            // starts below the hairline, which sits on the heading's own bottom edge). Its first 8 px are
                            // solid, so the cut-off tops of the captions passing under never show as torn letters.
                            stuck
                              ? "border-border after:from-background after:pointer-events-none after:absolute after:inset-x-0 after:top-[calc(100%+1px)] after:h-5 after:bg-linear-to-b after:from-40% after:to-transparent"
                              : "border-transparent",
                          )}
                        >
                          {group.key === "undated" ? (
                            group.label
                          ) : (
                            <time dateTime={group.key} title={libraryFullDate(group.key)}>
                              {group.label}
                            </time>
                          )}
                        </h3>
                      )}
                      {layout === "list" ? (
                        // Rows reach 8 px left of the title's edge, so the icon inside their padding lines up with it.
                        <ul className="-mx-2 flex flex-col gap-1">
                          {group.items.map((asset) => {
                            const title = libraryTitle(asset, noun);
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
                                  aria-current={isCurrent(asset) ? "true" : undefined}
                                  onClick={(event) => {
                                    noteLibraryTileClick(event);
                                    onPick(asset);
                                  }}
                                  className={cn(
                                    "hover:bg-border rounded-media flex min-h-14 w-full cursor-pointer items-center gap-3 p-2 text-left transition-colors",
                                    FOCUS_RING,
                                  )}
                                >
                                  <span className="rounded-media bg-muted/40 dark:bg-muted text-muted-foreground flex size-10 shrink-0 items-center justify-center border dark:border-foreground/20">
                                    <Icon aria-hidden="true" className="size-5" />
                                  </span>
                                  <span className="min-w-0 flex-1">
                                    <span title={title} className="line-clamp-2 text-sm font-medium break-words">
                                      {title}
                                    </span>
                                    {length && <span className="text-muted-foreground text-label block">{length}</span>}
                                  </span>
                                  {isCurrent(asset) && currentMark}
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                      ) : (
                        <ul className="grid gap-x-2 gap-y-3 [grid-template-columns:repeat(var(--span),minmax(0,1fr))] max-sm:grid-cols-3!">
                          {group.items.map((asset) => {
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
                                  aria-current={isCurrent(asset) ? "true" : undefined}
                                  onClick={(event) => {
                                    noteLibraryTileClick(event);
                                    onPick(asset);
                                  }}
                                  className={cn("rounded-media group flex w-full cursor-pointer flex-col gap-1 text-left", FOCUS_RING)}
                                >
                                  <span
                                    className={cn(
                                      "rounded-media bg-muted/40 relative flex aspect-square w-full items-center justify-center overflow-hidden border transition-colors",
                                      isCurrent(asset) ? "border-primary border-2" : "group-hover:border-foreground/40",
                                    )}
                                  >
                                    {isCurrent(asset) && <span className="absolute top-1 right-1 z-10">{currentMark}</span>}
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
                                  {/* Inner padding, so the label's first letter is well clear of the focus ring around the tile.
                                      A step quieter than the day heading above the tiles. Two lines are always kept, so every
                                      row of tiles has one height however its names wrap. */}
                                  <span title={title} className="text-label text-foreground line-clamp-2 min-h-[2lh] px-1.5 break-words">
                                    {title}
                                  </span>
                                  {length && (
                                    <span className="text-muted-foreground text-label -mt-1 block px-1.5">{length}</span>
                                  )}
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </section>
                  );
                })}
              </div>
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
          {/* "More below": the tiles fade into the dialog's own surface. Drawn over them; a shadow inside the
              scroll area would paint under the tiles and show as a grey band in the gaps. */}
          <div
            aria-hidden="true"
            data-slot="library-picker-fade"
            className="from-background pointer-events-none absolute inset-x-0 bottom-0 hidden h-6 bg-linear-to-t to-transparent peer-data-[more-below]:block"
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
