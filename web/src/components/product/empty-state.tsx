import type { ReactNode } from "react";
import { Inbox, type LucideIcon } from "lucide-react";

export interface EmptyStateProps {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: LucideIcon;
}

/**
 * A blank slate in the page's own rhythm: left-aligned, quiet, with the one
 * action that fills it (Geist 12c.2), not a centred stock illustration.
 */
export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Inbox,
}: EmptyStateProps) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-3 rounded-panel border border-dashed p-5">
      <Icon aria-hidden="true" className="text-subtle-foreground size-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {description ? (
          <p className="text-muted-foreground text-sm">{description}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}
