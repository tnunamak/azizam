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

/** Rows sit on an 8 px grid: 24 px lines and 8 px above and below, so a row is 40 or 64 px tall. */
const ROW = "text-meta flex items-start gap-3 py-2 leading-6";
/** A 1 px rule that takes no height, so the 8 px grid holds across groups. */
const RULE = "shadow-[inset_0_1px_0_var(--color-border)]";

/**
 * One thing that needs the owner: its state icon, the fact, the cause when known; the whole row opens the
 * fix. Something turned off is quieter than a fault: muted, regular weight, beside the working line.
 */
function NeedRow({ need }: { need: GatewayNeed }) {
  const { icon: Icon, className } = TONE[need.tone];
  const quiet = need.tone === "off";
  return (
    <li>
      <Link
        prefetch={false}
        href={need.action.href}
        aria-label={`${need.fact.replace(/\.$/, "")}. ${need.action.label}`}
        className={cn(ROW, "hover:bg-muted -mx-2 rounded-control px-2")}
      >
        <Icon aria-hidden="true" className={cn("mt-1 size-4 shrink-0", className)} />
        <span className="min-w-0 flex-1">
          <span className={cn("block", quiet ? "text-muted-foreground" : "font-medium")}>{need.fact}</span>
          {need.detail ? <span className="text-muted-foreground block">{need.detail}</span> : null}
        </span>
        <ChevronRight aria-hidden="true" className="text-muted-foreground mt-1 size-4 shrink-0" />
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
  const faults = needs.filter((need) => need.tone !== "off");
  const off = needs.filter((need) => need.tone === "off");
  const shown = faults.slice(0, SHOWN);
  const more = faults.length - shown.length;
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
          <ul>
            {shown.map((need) => (
              <NeedRow key={need.key} need={need} />
            ))}
          </ul>
        ) : null}
        {more > 0 ? (
          <Link
            prefetch={false}
            href="/status/"
            className={cn(ROW, "text-link font-medium underline-offset-4 hover:underline")}
          >
            {more} more in Status
          </Link>
        ) : null}
        {/* The quiet group: what is turned off, then what works. */}
        <div className={cn(shown.length > 0 && cn(RULE, "mt-2 pt-2"))}>
          {off.length ? (
            <ul>
              {off.map((need) => (
                <NeedRow key={need.key} need={need} />
              ))}
            </ul>
          ) : null}
          {working || !needs.length ? (
            <p className={cn(ROW, "text-muted-foreground")}>
              <CircleCheck aria-hidden="true" className="text-success mt-1 size-4 shrink-0" />
              <span>{working ?? QUIET}</span>
            </p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
