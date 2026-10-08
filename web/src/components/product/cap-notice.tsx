"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/relative-time";
import { Callout } from "@/components/product/status-ui";
import { capLimitFrom } from "@/app/home/presentation";

function nextMonthStart(now: Date): Date {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
}

/**
 * A spending-limit refusal that can still be in force: no later paid
 * success, and its limit cannot have reset yet. It says what happened, in
 * the past tense, when it can clear, and who acts. The owner gets the
 * gateway's words and the place to act; a member gets plain words.
 */
export function CapNotice({
  warning,
  at,
  owner,
}: {
  warning: string;
  at: number;
  owner: boolean;
}) {
  const limit = capLimitFrom(warning);
  const resets = nextMonthStart(new Date()).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const when =
    limit === "monthly"
      ? `The monthly limit resets on ${resets}.`
      : "The daily limit counts the last 24 hours, so requests can work again within a day.";
  if (owner)
    return (
      <Callout
        severity="attention"
        title="A spending limit refused paid requests"
        meta={
          <>
            Latest <RelativeTime ts={at} fallback="" />
          </>
        }
        actions={
          <Button asChild size="sm" variant="outline">
            <Link href="/status/">Open Status</Link>
          </Button>
        }
      >
        <p>
          {warning} {when}
        </p>
      </Callout>
    );
  return (
    <Callout
      severity="attention"
      title="Paid models were paused for you"
      meta={
        <>
          Latest <RelativeTime ts={at} fallback="" />
        </>
      }
    >
      <p>
        The household reached its {limit === "monthly" ? "monthly" : "daily"}{" "}
        limit for outside AI services. {when} Ask the gateway owner if you need
        them sooner.
      </p>
    </Callout>
  );
}
