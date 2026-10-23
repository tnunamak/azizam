"use client";

import Link from "next/link";
import { ChevronRight, CircleAlert, CircleCheck, CircleMinus, CircleX } from "lucide-react";
import { cn } from "@/lib/cn";
import { SectionHeading } from "@/components/product/readiness";
import type { GatewayNeed } from "./home-model";

/** How many "Needs you" rows show before the rest collapse into a link to Status. */
const SHOWN = 3;
const QUIET = "Everything is working";

const TONE = {
  down: { icon: CircleX, className: "text-danger" },
  degraded: { icon: CircleAlert, className: "text-warning" },
  off: { icon: CircleMinus, className: "text-subtle-foreground" },
} as const;

/** One thing that needs the owner: its state icon, the fact, the cause when known; the whole row opens the fix. */
function NeedRow({ need }: { need: GatewayNeed }) {
  const { icon: Icon, className } = TONE[need.tone];
  return (
    <li>
      <Link
        prefetch={false}
        href={need.action.href}
        aria-label={`${need.fact.replace(/\.$/, "")}. ${need.action.label}`}
        className="hover:bg-muted -mx-2 flex items-start gap-3 rounded-control px-2 py-2.5"
      >
        <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", className)} />
        <span className="min-w-0 flex-1">
          <span className="text-meta block font-medium">{need.fact}</span>
          {need.detail ? <span className="text-muted-foreground text-meta mt-0.5 block">{need.detail}</span> : null}
        </span>
        <ChevronRight aria-hidden="true" className="text-muted-foreground mt-0.5 size-4 shrink-0" />
      </Link>
    </li>
  );
}

/**
 * The owner's gateway panel: what needs the owner, each row one link to where it is fixed, then one line
 * for everything that works. Operational facts only; it never shows anyone's prompts, titles, pictures or
 * names beside content. On a phone it is one row that opens Status.
 */
export function OwnerPanel({ needs, working }: { needs: GatewayNeed[]; working: string | null }) {
  const shown = needs.slice(0, SHOWN);
  const more = needs.length - shown.length;
  const first = needs[0];
  const FirstIcon = first ? TONE[first.tone].icon : CircleCheck;
  return (
    <section aria-labelledby="gateway-heading" className="min-w-0">
      <SectionHeading
        id="gateway-heading"
        title="Your gateway"
        className="max-lg:sr-only"
        action={
          <Link
            prefetch={false}
            href="/status/"
            className="text-link text-meta inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-lg:hidden"
          >
            Status
            <ChevronRight aria-hidden="true" className="size-4" />
          </Link>
        }
      />
      <Link
        prefetch={false}
        href="/status/"
        className="bg-card text-meta flex min-h-10 items-center gap-3 rounded-panel px-3 py-2 max-sm:min-h-11 lg:hidden"
      >
        <FirstIcon aria-hidden="true" className={cn("size-4 shrink-0", first ? TONE[first.tone].className : "text-success")} />
        <span className="min-w-0 flex-1 truncate font-medium">{first?.fact ?? QUIET}</span>
        <span className="text-link inline-flex shrink-0 items-center font-medium">
          Open Status
          <ChevronRight aria-hidden="true" className="size-4" />
        </span>
      </Link>
      <div className="bg-card rounded-panel px-4 py-2 max-lg:hidden">
        {shown.length ? (
          <ul className="divide-border divide-y">
            {shown.map((need) => (
              <NeedRow key={need.key} need={need} />
            ))}
          </ul>
        ) : null}
        {more > 0 ? (
          <Link
            prefetch={false}
            href="/status/"
            className="text-link text-meta flex items-center border-t py-3 font-medium underline-offset-4 hover:underline"
          >
            {more} more in Status
          </Link>
        ) : null}
        {working || !shown.length ? (
          <p className={cn("text-meta text-muted-foreground flex items-start gap-3 py-3", shown.length > 0 && "border-t")}>
            <CircleCheck aria-hidden="true" className="text-success mt-0.5 size-4 shrink-0" />
            <span>{working ?? QUIET}</span>
          </p>
        ) : null}
      </div>
    </section>
  );
}
