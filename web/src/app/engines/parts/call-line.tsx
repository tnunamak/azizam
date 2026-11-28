"use client";

import { CircleAlert, Info } from "lucide-react";
import type { EngineDefinition, EngineValues } from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { callLine } from "../add-engine/draft";

/** The first connection-check request, updated as the address changes. */
export function CallLine({
  definition,
  values,
}: {
  definition: EngineDefinition;
  values: Pick<EngineValues, "address" | "path_prefix">;
}) {
  const line = callLine(definition, values);
  const twice = line.kind === "call" && line.twice;
  const Icon = twice ? CircleAlert : Info;
  return (
    <p
      data-testid="call-line"
      className={cn("text-meta flex items-start gap-1.5", twice ? "text-warning" : "text-muted-foreground")}
    >
      <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {line.kind === "empty" && "Enter an address to see the first connection-check request."}
        {line.kind === "call" && (
          <>
            Connection check: <b className="font-mono font-medium">{line.request}</b>
            {line.twice &&
              `. The address already ends in ${line.twice}, so that part is sent twice. Clear the extra path or shorten the address.`}
          </>
        )}
      </span>
    </p>
  );
}
