"use client";

import { useId } from "react";
import { ChevronRight, Search } from "lucide-react";
import type { EngineDefinition } from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

function visibleText(d: EngineDefinition) {
  return `${d.title} ${d.example ?? ""} ${d.summary}`.toLowerCase();
}

/** Step 1: search over the definitions. Hidden keywords match too, and say so. */
export function StepKind({
  definitions,
  query,
  onQuery,
  onPick,
}: {
  definitions: EngineDefinition[];
  query: string;
  onQuery: (q: string) => void;
  onPick: (definition: EngineDefinition) => void;
}) {
  const searchId = useId();
  const q = query.trim().toLowerCase();
  const rows = definitions.filter(
    (d) => !q || `${visibleText(d)} ${(d.keywords ?? []).join(" ")}`.toLowerCase().includes(q),
  );
  const first = definitions[0];
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-heading">What kind of server is it?</h3>
        <p className="text-meta text-muted-foreground mt-0.5">
          Choose one to continue. Not sure? Pick the first. Azizam checks what the server can do.
        </p>
      </div>
      <div className="relative">
        <Search aria-hidden="true" className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
        <label htmlFor={searchId} className="sr-only">
          Search server kinds
        </label>
        <Input
          id={searchId}
          type="search"
          className="pl-9"
          placeholder="Search, for example Ollama or vLLM"
          value={query}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => onQuery(event.target.value)}
        />
      </div>
      {rows.length === 0 ? (
        <div role="status" className="rounded-panel text-meta border border-dashed p-4">
          No kind matches “{query.trim()}”. If the server speaks the OpenAI style, pick “{first?.title}”.
          {first && (
            <div className="mt-2">
              <Button type="button" variant="outline" size="sm" onClick={() => onPick(first)}>
                Use {first.title}
              </Button>
            </div>
          )}
        </div>
      ) : (
        <div role="group" aria-label="Server kinds" className="flex flex-col gap-2">
          {rows.map((d) => {
            const hidden = q && !visibleText(d).includes(q);
            return (
              <button
                key={d.definition}
                type="button"
                onClick={() => onPick(d)}
                className={cn(
                  "bg-card rounded-panel hover:border-input hover:bg-muted/40 flex w-full items-center gap-3 border p-4 text-left transition-colors",
                  !q && d === first && "border-primary/40",
                )}
              >
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="text-body flex flex-wrap items-center gap-2 font-medium">
                    {d.title}
                    {d.badge && (
                      <span className="rounded-badge text-badge bg-info-tint text-info inline-flex h-5 items-center px-1.5">{d.badge}</span>
                    )}
                  </span>
                  {d.example && <span className="text-meta text-muted-foreground">{d.example}</span>}
                  <span className="text-meta">{d.summary}</span>
                  {hidden && <span className="text-meta text-info">Matched on “{query.trim()}”</span>}
                </span>
                <ChevronRight aria-hidden="true" className="text-muted-foreground size-4 shrink-0" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
