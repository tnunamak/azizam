"use client";

import { useEffect, useRef, useState } from "react";
import { ImageIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/product";
import { kindIcon } from "@/lib/media-kind";
import { networkFetch } from "@/lib/network-error";
import {
  fileFitsTypes,
  mediaNoun,
  withArticle,
  type MediaTypes,
} from "@/lib/media-pick";
import type { LibraryAsset, LibraryPick } from "@/lib/library-file";

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
  const [state, setState] = useState<"idle" | "loading" | "error" | "ready">(
    "idle",
  );
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [more, setMore] = useState<"idle" | "loading" | "error">("idle");
  const [unshown, setUnshown] = useState<Set<string>>(new Set());
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

  const capitalNoun = `${noun[0].toUpperCase()}${noun.slice(1)}`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Choose {withArticle(noun)} from your Library</DialogTitle>
          <DialogDescription>{capitalNoun}s you made, newest first.</DialogDescription>
        </DialogHeader>
        {state === "loading" && (
          <div
            aria-busy="true"
            className="grid grid-cols-3 gap-2 sm:grid-cols-4"
          >
            {Array.from({ length: 8 }, (_, index) => (
              <Skeleton key={index} className="aspect-square w-full" />
            ))}
          </div>
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
        {state === "ready" && assets.length > 0 && (
          <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {assets.map((asset) => {
              const title =
                asset.prompt?.trim().slice(0, 80) ||
                `${capitalNoun} from your Library`;
              // The list's own signed links show the item; the download route
              // answers with a link, not an image.
              const link =
                (kind === "image" ? asset.thumbnail_url : null) ??
                asset.download_url;
              const src = link && !unshown.has(asset.asset_id) ? link : null;
              const drawn = kind === "image" || kind === "video";
              return (
                <li key={asset.asset_id}>
                  <button
                    type="button"
                    title={title}
                    aria-label={title}
                    onClick={() => onPick(asset)}
                    className="rounded-media focus-visible:ring-ring/50 bg-muted/40 flex aspect-square w-full items-center justify-center overflow-hidden border focus-visible:ring-[3px] focus-visible:outline-none"
                  >
                    {!drawn || !src ? (
                      <span className="text-muted-foreground flex flex-col items-center gap-1 p-1 text-xs">
                        {drawn ? (
                          <ImageIcon aria-hidden="true" className="size-6" />
                        ) : (
                          <Icon aria-hidden="true" className="size-6" />
                        )}
                        {drawn && "Preview unavailable"}
                        {!drawn && (
                          <span className="line-clamp-3">{title}</span>
                        )}
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
      </DialogContent>
    </Dialog>
  );
}
