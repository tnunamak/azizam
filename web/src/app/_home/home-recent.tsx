"use client";

import Link from "next/link";
import { ChevronRight, MessageSquare } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { RelativeTime } from "@/components/product/relative-time";
import { MAKING_COPY } from "@/components/product/making-tile";
import { capabilityMeta, SectionHeading } from "@/components/product/readiness";
import { MediaTile } from "./home-media-tile";
import { makingProgress, makingRisk, makingTimeLeft, type MakingKind as HomeMakingKind, type RecentItem } from "./home-model";
import type { CapabilityView } from "@/lib/capability-state";

/** Six tiles on a wide screen and four beside the owner's panel. A phone shows the first four. */
const WIDE_SHOWN = 6;
const OWNER_SHOWN = 4;
/**
 * From 1024 px the grid has one column per tile, so five or six tiles fill the row with no gap at its
 * end. Fewer than four tiles keep four columns, so one tile does not stretch across the page. From
 * 1280 px the page grid decides instead: six tracks that line up with the composer (GRID).
 */
const WIDE_COLUMNS = ["", "lg:grid-cols-4", "lg:grid-cols-4", "lg:grid-cols-4", "lg:grid-cols-4", "lg:grid-cols-5", "lg:grid-cols-6"];
/** From 1280 px the tiles sit on the page's twelve-column grid, two columns each (see home-page). */
const GRID = "grid grid-cols-2 gap-x-3 gap-y-5 sm:grid-cols-3 xl:grid-cols-6 xl:gap-x-4";
const GRID_OWNER = "grid grid-cols-2 gap-x-3 gap-y-5 sm:grid-cols-4 lg:grid-cols-4 xl:gap-x-4";
const PHONE_BEYOND_FOUR = "max-sm:hidden";

type Making = Extract<RecentItem, { kind: "making" }>;

/** Skeletons at the size of the tiles, so the page does not move when they fill. */
export function RecentLoading() {
  return (
    <div role="status" aria-label="Loading recent work" aria-busy="true" className={cn(GRID, WIDE_COLUMNS[WIDE_SHOWN])}>
      {[0, 1, 2, 3, 4, 5].map((item) => (
        <div key={item} className={cn(item >= 4 && "max-sm:hidden")}>
          <Skeleton className="rounded-media aspect-square w-full motion-reduce:animate-none" />
          <Skeleton className="mt-2 h-4 w-4/5 motion-reduce:animate-none" />
        </div>
      ))}
    </div>
  );
}

/** The capability key whose glyph and hue a running make wears. */
const MAKING_CAPABILITY: Record<HomeMakingKind, string> = { image: "image", video: "video", audio: "speech" };

/** A make that is still running: its tile first, with the time left, then the result. */
function MakingTileView({ item, now, risk, className }: { item: Making; now: number; risk: string | null; className?: string }) {
  const progress = makingProgress(item, now);
  const timeLeft = makingTimeLeft(item, now);
  const overdue = timeLeft === "Taking longer than usual";
  const making = item.making as HomeMakingKind;
  const copy = MAKING_COPY[making];
  const { icon: Icon, tone, tint } = capabilityMeta(MAKING_CAPABILITY[making]);
  return (
    <li className={cn("min-w-0", className)} data-slot="recent-tile" data-making="true">
      <div
        role="status"
        aria-label={copy}
        className="bg-card border-border rounded-media relative flex aspect-square w-full items-center justify-center overflow-hidden border"
      >
        <span className={cn("flex size-12 items-center justify-center rounded-full", tint)}>
          <Icon aria-hidden="true" className={cn("size-6 animate-pulse motion-reduce:animate-none", tone)} />
        </span>
        {progress != null ? (
          <div
            role="progressbar"
            aria-label={`${copy} progress`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
            className="bg-border absolute inset-x-3 bottom-3 h-1 overflow-hidden rounded-full"
          >
            <div className="bg-foreground/70 h-full rounded-full" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        ) : null}
      </div>
      <p className="text-foreground text-meta mt-2 truncate font-medium">{copy}</p>
      <p className="text-muted-foreground text-badge truncate">{overdue ? timeLeft : timeLeft ? capitalise(timeLeft) : "Making"}</p>
      {risk ? <p className="text-warning text-badge">{risk}</p> : null}
    </li>
  );
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * A conversation whose body request history kept. The face holds the person's own words, so the caption
 * names the kind instead of repeating them. It opens the receipt.
 */
function ChatTileView({ item, className }: { item: Extract<RecentItem, { kind: "chat" }>; className?: string }) {
  const { chat } = item;
  return (
    <li className={cn("min-w-0", className)} data-slot="recent-tile">
      <Link
        prefetch={false}
        href={`/usage/?receipt=${encodeURIComponent(chat.request_id)}`}
        aria-label={`Open chat: ${chat.title}`}
        className="group block min-w-0"
      >
        <div className="bg-card border-border group-hover:border-input rounded-media flex aspect-square w-full flex-col overflow-hidden border p-4 transition-colors duration-(--motion-fast)">
          <MessageSquare aria-hidden="true" className="text-capability-chat size-4 shrink-0" />
          <p className="text-foreground text-meta mt-3 line-clamp-6">{chat.title}</p>
        </div>
        <span className="mt-2 block min-w-0">
          <span className="text-foreground text-meta block truncate font-medium">Chat</span>
          <span className="text-muted-foreground text-badge block truncate">
            <RelativeTime ts={chat.created_at} fallback="" />
          </span>
        </span>
      </Link>
    </li>
  );
}

/**
 * The person's recent work: makes still running, then finished pictures, audio and video, then
 * conversations, newest first. Six tiles on a wide screen, four beside the owner's panel and on a
 * phone. A failed media read says so here and leaves the composer working.
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
  const shown = items.slice(0, owner ? OWNER_SHOWN : WIDE_SHOWN);
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
          <ul aria-label="Recent work" className={owner ? GRID_OWNER : cn(GRID, WIDE_COLUMNS[shown.length])}>
            {shown.map((item, index) => {
              const className = index >= 4 ? PHONE_BEYOND_FOUR : undefined;
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
