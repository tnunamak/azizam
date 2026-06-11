import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface EmptyStateProps {
  icon?: ReactNode;
  /** One quiet line, e.g. "No apps yet." */
  children: ReactNode;
  /** Usually the same create action as the section header. */
  action?: ReactNode;
  className?: string;
}

/** Light empty state — never a heavy bordered box (steering doc). */
export function EmptyState({
  icon,
  children,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 py-10 text-center",
        className,
      )}
    >
      {icon && <div className="text-vf-subtle">{icon}</div>}
      <p className="text-vf-body text-vf-muted">{children}</p>
      {action && <div>{action}</div>}
    </div>
  );
}
