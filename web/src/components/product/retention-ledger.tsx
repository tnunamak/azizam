"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export interface RetentionFact {
  id: string;
  holds: string;
  where: string;
  how_long: string;
  default: string;
  condition?: string | null;
  reach: string;
  control: string;
  evidence_url?: string | null;
  checked: string;
  confidence: string;
  stale: boolean;
  source?: string | null;
  revision?: string | null;
}

export interface RetentionLedger {
  kinds: Array<{
    id: string;
    purpose: string;
    readers: string;
    reader_explanation?: string;
    decision?: string;
    kept_until_ms?: number | null;
  }>;
  destinations: Array<{
    id: string;
    name: string;
    kind: string;
    address?: string | null;
    reach?: string | null;
    facts: RetentionFact[];
  }>;
  never_save_text: string;
}

function Fact({ fact }: { fact: RetentionFact }) {
  return (
    <li className="space-y-2 border-t pt-3 first:border-t-0 first:pt-0">
      <p className="font-medium">{fact.holds}</p>
      <dl className="text-meta grid gap-x-4 gap-y-1 sm:grid-cols-[7rem_minmax(0,1fr)]">
        <dt className="text-muted-foreground">Where</dt>
        <dd>{fact.where}</dd>
        <dt className="text-muted-foreground">How long</dt>
        <dd>{fact.how_long}</dd>
        <dt className="text-muted-foreground">Default</dt>
        <dd>{fact.default}</dd>
        {fact.condition && (
          <>
            <dt className="text-muted-foreground">When</dt>
            <dd>{fact.condition}</dd>
          </>
        )}
        <dt className="text-muted-foreground">Who can reach it</dt>
        <dd>{fact.reach}</dd>
        <dt className="text-muted-foreground">What you can do</dt>
        <dd>{fact.control}</dd>
      </dl>
      <p className="text-meta text-muted-foreground">
        {fact.stale
          ? `This was last checked on ${fact.checked}.`
          : `Checked ${fact.checked}.`}
        {fact.source === "set by the owner" && " Set by the owner."}
        {fact.evidence_url && (
          <>
            {" "}
            <a
              className="text-link underline underline-offset-2"
              href={fact.evidence_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Source
            </a>
          </>
        )}
      </p>
    </li>
  );
}

/** One view for household settings and an individual engine. Facts are server data. */
export function RetentionLedgerPanel({
  endpoint,
  id,
  title = "Who could see a request",
}: {
  endpoint: string;
  id: string;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["retention-ledger", endpoint],
    queryFn: () => adminFetch<{ ledger: RetentionLedger }>(endpoint),
    enabled: open,
  });
  const ledger = data?.ledger;

  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="scroll-mt-20 space-y-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={`${id}-title`} className="text-heading">
          {title}
        </h2>
        <Button
          type="button"
          variant="outline"
          aria-expanded={open}
          aria-controls={`${id}-body`}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Hide ledger" : "View ledger"}
        </Button>
      </div>
      <p className="text-meta text-muted-foreground">
        Where request content can land, and who may read it. Facts come from
        saved settings and dated sources.
      </p>
      {open && (
        <div
          id={`${id}-body`}
          className="bg-card rounded-panel space-y-6 border p-4 sm:p-5"
        >
          {isPending ? (
            <div
              role="status"
              aria-label="Loading ledger"
              aria-busy="true"
              className="space-y-3"
            >
              <Skeleton className="h-5 w-2/3" />
              <Skeleton className="h-20 w-full" />
            </div>
          ) : isError || !ledger ? (
            <div role="alert" className="space-y-2">
              <p>The ledger could not be loaded.</p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void refetch()}
              >
                Try again
              </Button>
            </div>
          ) : (
            <>
              <div className="space-y-3">
                <h3 className="font-medium">What Azizam keeps</h3>
                {ledger.kinds.length ? (
                  <ul className="space-y-3">
                    {ledger.kinds.map((kind) => (
                      <li key={kind.id} className="text-meta space-y-1">
                        <p className="font-medium">{kind.id.replaceAll("_", " ")}</p>
                        <p>{kind.purpose}</p>
                        <p>{kind.decision ?? "Whether this is kept depends on the request and your settings."}</p>
                        <p className="text-muted-foreground">
                          Readers: {kind.reader_explanation ?? kind.readers}
                          {kind.kept_until_ms ? ` · Until ${new Date(kind.kept_until_ms).toLocaleString()}` : ""}
                        </p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-meta text-muted-foreground">
                    No kept kinds are listed.
                  </p>
                )}
              </div>
              <div className="space-y-4">
                <h3 className="font-medium">Where it can go</h3>
                {ledger.destinations.length ? (
                  ledger.destinations.map((destination) => (
                    <section
                      key={destination.id}
                      aria-label={destination.name}
                      className="space-y-3 border-t pt-4"
                    >
                      <div>
                        <h4 className="font-medium">{destination.name}</h4>
                        <p className="text-meta text-muted-foreground">
                          {destination.kind}
                          {destination.address
                            ? ` · Azizam reaches it at ${destination.address}`
                            : ""}
                          {destination.reach ? ` · ${destination.reach}` : ""}
                        </p>
                      </div>
                      {destination.facts.length ? (
                        <ul className="space-y-3">
                          {destination.facts.map((fact) => (
                            <Fact key={fact.id} fact={fact} />
                          ))}
                        </ul>
                      ) : (
                        <p className="text-meta text-muted-foreground">
                          No retention facts recorded for this destination.
                        </p>
                      )}
                    </section>
                  ))
                ) : (
                  <p className="text-meta text-muted-foreground">
                    No destinations are listed.
                  </p>
                )}
              </div>
              {ledger.never_save_text && (
                <div className="space-y-2 border-t pt-4">
                  <h3 className="font-medium">If you choose never save</h3>
                  <p className="text-meta whitespace-pre-line">
                    {ledger.never_save_text}
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
