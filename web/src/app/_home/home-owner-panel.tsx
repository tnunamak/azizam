"use client";

import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { GatewayNeed } from "./home-model";

/** How many "Needs you" items show before the rest collapse into a link to Status. */
const SHOWN = 2;
const QUIET = "Everything is working";

/**
 * The owner's gateway panel: what needs the owner, one action each. When nothing does, one quiet
 * line and the link to Status. Operational facts only; it never shows anyone's prompts, titles,
 * pictures or names beside content. On a phone it is one row that opens Status.
 */
export function OwnerPanel({ needs }: { needs: GatewayNeed[] }) {
  const shown = needs.slice(0, SHOWN);
  const more = needs.length - shown.length;
  return (
    <section aria-labelledby="gateway-heading" className="bg-card rounded-control border p-4">
      <h2 id="gateway-heading" className="text-heading max-lg:sr-only">
        Your gateway
      </h2>
      <Link
        prefetch={false}
        href="/status/"
        className="text-meta mt-1 flex min-h-11 items-center justify-between gap-2 lg:hidden"
      >
        <span className="min-w-0">{needs[0]?.fact ?? QUIET}</span>
        <span className="text-link inline-flex shrink-0 items-center font-medium">
          Open Status
          <ChevronRight aria-hidden="true" className="size-4" />
        </span>
      </Link>
      <div className="max-lg:hidden">
        {shown.length ? (
          <ul className="mt-3 divide-y">
            {shown.map((need) => (
              <li key={need.key} className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0">
                <p className="text-meta min-w-0 flex-1">{need.fact}</p>
                <Button asChild variant="outline" size="sm">
                  <Link prefetch={false} href={need.action.href}>
                    {need.action.label}
                  </Link>
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-meta mt-3 flex items-center justify-between gap-2">
            <span>{QUIET}</span>
            <Link
              prefetch={false}
              href="/status/"
              className="text-link inline-flex shrink-0 items-center font-medium underline-offset-4 hover:underline"
            >
              Open Status
            </Link>
          </p>
        )}
        {more > 0 ? (
          <Link prefetch={false} href="/status/" className="text-link text-meta mt-2 inline-flex items-center font-medium underline-offset-4 hover:underline">
            +{more} more in Status
          </Link>
        ) : null}
      </div>
    </section>
  );
}
