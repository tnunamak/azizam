import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface SectionHeaderProps {
  /** Mono uppercase kicker above the title, e.g. "access". */
  kicker?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  /** Action slot — typically the section's one accent Button. */
  action?: ReactNode;
  className?: string;
}

export function SectionHeader({
  kicker,
  title,
  description,
  action,
  className,
}: SectionHeaderProps) {
  return (
    <header
      className={cn(
        "flex flex-wrap items-end justify-between gap-3",
        className,
      )}
    >
      <div className="flex min-w-0 flex-col gap-1">
        {kicker && (
          <p className="text-vf-micro text-vf-subtle font-mono uppercase">
            {kicker}
          </p>
        )}
        <h2 className="text-vf-title text-vf-text font-semibold">{title}</h2>
        {description && (
          <p className="text-vf-small text-vf-muted">{description}</p>
        )}
      </div>
      {action && (
        <div className="flex shrink-0 items-center gap-2">{action}</div>
      )}
    </header>
  );
}
