import type { ReactNode } from "react";

import { cn } from "@/lib/cn";
import { Badge, type BadgeTone } from "./badge";

export interface SummaryStripItem {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  tone?: BadgeTone;
  badge?: ReactNode;
}

export interface SummaryStripProps {
  items: SummaryStripItem[];
  className?: string;
}

/** Quiet context strip for page-level counts.
 *
 * This intentionally avoids dashboard tiles: one surface, compact values,
 * and hairline dividers. Use it when counts help an operator orient before a
 * list, not as a substitute for the list itself.
 */
export function SummaryStrip({ items, className }: SummaryStripProps) {
  return (
    <div
      className={cn(
        "vf-metric-strip md:grid-cols-[repeat(auto-fit,minmax(9rem,1fr))]",
        className,
      )}
    >
      {items.map((item, index) => (
        <div key={index} className="vf-metric">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="vf-metric-label">{item.label}</span>
            {item.detail && (
              <span className="vf-metric-detail truncate">{item.detail}</span>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <span className="vf-metric-value tabular-nums">{item.value}</span>
            {item.badge && (
              <Badge tone={item.tone ?? "neutral"} sm>
                {item.badge}
              </Badge>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
