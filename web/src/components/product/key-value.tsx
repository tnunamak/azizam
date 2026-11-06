import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export type KeyValueItem = [key: string, value: ReactNode];

/**
 * Facts on a detail page, a receipt or a Status card (DESIGN "List, table
 * and card anatomy"; Stripe Workbench's detail rows): rows 32–36, the key in
 * `text-meta` muted in a 160 column, the value in `text-body`. On a phone
 * the key sits above its value.
 */
export function KeyValueList({
  items,
  className,
}: {
  items: KeyValueItem[];
  className?: string;
}) {
  return (
    <dl
      data-slot="key-value-list"
      className={cn(
        "grid min-w-0 grid-cols-1 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-x-4",
        className,
      )}
    >
      {items.map(([key, value]) => (
        <div key={key} data-slot="key-value-row" className="contents">
          <dt className="text-muted-foreground text-meta pt-1.5 sm:min-h-8 sm:py-1.5">
            {key}
          </dt>
          <dd className="text-body min-w-0 break-words max-sm:pb-1.5 sm:min-h-8 sm:py-1">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
