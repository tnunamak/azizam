import { useId, type ReactNode } from "react";
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
  /**
   * Drawn in a region that already pads its content (a dialog body): no
   * padding or minimum height of its own, so the icon lines up with the
   * region's title and the region fits the state.
   */
  flush?: boolean;
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
  flush = false,
}: EmptyStateProps) {
  const titleId = useId();
  const descriptionId = useId();
  return (
    <div
      data-slot="empty-state"
      role="region"
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      className={cn(
        "flex min-w-0 flex-col items-start gap-3",
        flush ? "justify-start pt-1 pb-2" : "min-h-40 justify-center p-6 max-sm:p-4",
        !inCard && !flush && "rounded-panel border border-dashed",
      )}
    >
      <Icon
        aria-hidden="true"
        className="text-muted-foreground size-5 shrink-0"
      />
      <div className="min-w-0 space-y-1">
        <p id={titleId} className="text-body font-medium">{title}</p>
        {description ? (
          <p id={descriptionId} className="text-muted-foreground text-meta">{description}</p>
        ) : null}
      </div>
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
}
