import type { ReactNode } from "react";
import { Inbox, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

export interface EmptyStateProps {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: LucideIcon;
  /**
   * Drawn inside a card that already has an edge (a ListCard): no dashed
   * border of its own.
   */
  inCard?: boolean;
}

/**
 * A blank slate in the region it would fill (DESIGN "Empty, loading and
 * error"; Geist 12c.2): left-aligned, a 20 icon in muted ink, the title in
 * `text-body` 500, one sentence in `text-meta`, the one action that fills it
 * at 32, at least 160 tall. A dashed hairline only when the region has no
 * card of its own. Never a centred stock illustration.
 */
export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Inbox,
  inCard = false,
}: EmptyStateProps) {
  return (
    <div
      data-slot="empty-state"
      className={cn(
        "flex min-h-40 min-w-0 flex-col items-start justify-center gap-3 p-6 max-sm:p-4",
        !inCard && "rounded-panel border border-dashed",
      )}
    >
      <Icon
        aria-hidden="true"
        className="text-muted-foreground size-5 shrink-0"
      />
      <div className="min-w-0 space-y-1">
        <p className="text-body font-medium">{title}</p>
        {description ? (
          <p className="text-muted-foreground text-meta">{description}</p>
        ) : null}
      </div>
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
}
