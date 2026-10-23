"use client";

import Link from "next/link";
import { ChevronRight, Loader2, MessageSquare } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/product/relative-time";
import { MAKING_COPY } from "@/components/product/making-tile";
import { SectionHeading } from "@/components/product/readiness";
import { MediaTile } from "./home-media-tile";
import { makingProgress, makingRisk, makingTimeLeft, type MakingKind as HomeMakingKind, type RecentItem } from "./home-model";
import type { CapabilityView } from "@/lib/capability-state";

/** Six tiles on a wide screen and four on a phone. Beside the owner's panel, four at every width from large up. */
const TILE_BEYOND_FOUR = "max-sm:hidden";
const OWNER_TILE_BEYOND_FOUR = "max-sm:hidden lg:hidden";
const GRID = "grid grid-cols-2 gap-x-3 gap-y-5 sm:grid-cols-3";
const GRID_WIDE = "lg:grid-cols-6";
const GRID_OWNER = "lg:grid-cols-4";

type Making = Extract<RecentItem, { kind: "making" }>;

/** Skeletons at the size of the tiles, so the page does not move when they fill. */
export function RecentLoading() {
  return (
    <div role="status" aria-label="Loading recent work" aria-busy="true" className={cn(GRID, GRID_WIDE)}>
      {[0, 1, 2, 3, 4, 5].map((item) => (
        <div key={item} className={cn(item >= 4 && "max-sm:hidden")}>
          <Skeleton className="rounded-media aspect-square w-full motion-reduce:animate-none" />
          <Skeleton className="mt-2 h-4 w-4/5 motion-reduce:animate-none" />
        </div>
      ))}
    </div>
  );
}

/** A make that is still running: its tile first, with the time left, then the result. */
function MakingTileView({ item, now, risk, className }: { item: Making; now: number; risk: string | null; className?: string }) {
  const progress = makingProgress(item, now);
  const timeLeft = makingTimeLeft(item, now);
  const overdue = timeLeft === "Taking longer than usual";
  const copy = MAKING_COPY[item.making as HomeMakingKind];
  return (
    <li className={cn("min-w-0", className)} data-slot="recent-tile" data-making="true">
      <div
        role="status"
        aria-label={copy}
        className="bg-muted rounded-media flex aspect-square w-full flex-col items-center justify-center gap-3 p-3"
      >
        <Loader2 aria-hidden="true" className="text-muted-foreground size-6 animate-spin motion-reduce:animate-none" />
        {progress != null ? (
          <div
            role="progressbar"
            aria-label={`${copy} progress`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
            className="bg-border h-1 w-3/4 overflow-hidden rounded-full"
          >
            <div className="bg-foreground h-full rounded-full" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        ) : null}
      </div>
      <p className="text-foreground text-meta mt-2 truncate font-medium">{copy}</p>
      <p className="text-muted-foreground text-badge truncate">
        {overdue ? timeLeft : `Making${timeLeft ? ` · ${timeLeft}` : ""}`}
      </p>
      {risk ? <p className="text-warning text-badge">{risk}</p> : null}
    </li>
  );
}

/** A conversation whose body request history kept: its first words, and a link to its receipt. */
function ChatTileView({ item, className }: { item: Extract<RecentItem, { kind: "chat" }>; className?: string }) {
  const { chat } = item;
  return (
    <li className={cn("min-w-0", className)} data-slot="recent-tile">
      <Link prefetch={false} href={`/usage/?receipt=${encodeURIComponent(chat.request_id)}`} className="group block min-w-0">
        <div className="bg-muted rounded-media flex aspect-square w-full flex-col gap-2 overflow-hidden p-3">
          <MessageSquare aria-hidden="true" className="text-muted-foreground size-4 shrink-0" />
          <p className="text-foreground text-meta line-clamp-4">{chat.title}</p>
        </div>
        <span className="mt-2 block min-w-0">
          <span className="text-foreground text-meta block truncate font-medium group-hover:underline">{chat.title}</span>
          <span className="text-muted-foreground text-badge block truncate">
            Chat
            <span aria-hidden="true"> · </span>
            <RelativeTime ts={chat.created_at} fallback="" />
          </span>
        </span>
      </Link>
    </li>
  );
}

/**
 * The person's recent work: makes still running, then finished pictures, audio and video, then
 * conversations, newest first. Six tiles on a wide screen, four on a phone (and beside the owner's
 * panel). A failed media read says so here and leaves the composer working.
 */
export function Recent({
  items,
  now,
  views,
  owner,
  mediaState,
  onRetry,
  retrying,
}: {
  items: RecentItem[];
  now: number;
  /** Each capability's state, so a make on a capability that is not working says so. */
  views: CapabilityView[];
  owner: boolean;
  mediaState: "pending" | "error" | "ready";
  onRetry: () => void;
  retrying: boolean;
}) {
  const tileClass = owner ? OWNER_TILE_BEYOND_FOUR : TILE_BEYOND_FOUR;
  return (
    <section aria-labelledby="recent-heading">
      <SectionHeading
        id="recent-heading"
        title="Recent"
        action={
          <Link
            prefetch={false}
            className="text-link text-meta inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
            href="/library/"
          >
            Library
            <ChevronRight aria-hidden="true" className="size-4" />
          </Link>
        }
      />
      {!items.length && mediaState === "pending" ? <RecentLoading /> : null}
      {!items.length && mediaState === "error" ? (
        <div className="rounded-control bg-card flex flex-wrap items-center justify-between gap-3 border p-3">
          <p className="text-meta" role="alert">
            Couldn’t load your recent work.
          </p>
          <Button variant="outline" size="sm" onClick={onRetry} disabled={retrying}>
            Try again
          </Button>
        </div>
      ) : null}
      {items.length ? (
        <>
          {mediaState === "error" ? (
            <div className="text-meta mb-3 flex flex-wrap items-center gap-3">
              <span role="alert">Some recent work didn’t load.</span>
              <Button variant="outline" size="sm" onClick={onRetry} disabled={retrying}>
                Try again
              </Button>
            </div>
          ) : null}
          <ul aria-label="Recent work" className={cn(GRID, owner ? GRID_OWNER : GRID_WIDE)}>
            {items.map((item, index) => {
              const className = index >= 4 ? tileClass : undefined;
              if (item.kind === "making")
                return (
                  <MakingTileView key={item.key} item={item} now={now} risk={makingRisk(item, views)} className={className} />
                );
              if (item.kind === "chat")
                return <ChatTileView key={item.key} item={item} className={className} />;
              return <MediaTile key={item.key} creation={item.asset} className={className} />;
            })}
          </ul>
        </>
      ) : null}
    </section>
  );
}
