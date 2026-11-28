"use client";

import { useId, useState } from "react";
import changelog from "@/data/whats-new.json";
import { Button } from "@/components/ui/button";
import { ListCard } from "./list-card";

type Entry = { date: string; title: string; detail: string };

const INITIAL_COUNT = 3;
const dayFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

export function WhatsNew({
  entries = changelog.entries,
}: {
  entries?: readonly Entry[];
}) {
  const headingId = useId();
  const [expanded, setExpanded] = useState(false);

  if (entries.length === 0) return null;

  return (
    <section
      aria-labelledby={headingId}
      className="page-read mt-(--space-section) min-w-0"
    >
      <h2 id={headingId} className="text-heading mb-2">
        {"What's new"}
      </h2>
      <ListCard labelledBy={headingId}>
        {(expanded ? entries : entries.slice(0, INITIAL_COUNT)).map((entry) => (
          <li key={entry.date + ":" + entry.title} className="min-w-0 px-4 py-3">
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <h3 className="text-body font-medium">{entry.title}</h3>
              <time
                dateTime={entry.date}
                className="text-meta text-muted-foreground shrink-0"
              >
                {dayFormat.format(new Date(entry.date + "T00:00:00Z"))}
              </time>
            </div>
            <p className="text-meta text-muted-foreground mt-1">{entry.detail}</p>
          </li>
        ))}
      </ListCard>
      {entries.length > INITIAL_COUNT && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show fewer" : `Show all (${entries.length})`}
        </Button>
      )}
    </section>
  );
}
