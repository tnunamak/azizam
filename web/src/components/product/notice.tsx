import type { ReactNode } from "react";
import { CircleAlert } from "lucide-react";
import { cn } from "@/lib/cn";

/** A one-fact warning under a verdict: what happened, the evidence, one action. */
export function Notice({
  title,
  children,
  action,
  className,
}: {
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn(
        "bg-warning-tint flex min-w-0 items-start gap-3 rounded-panel p-4",
        className,
      )}
    >
      <CircleAlert aria-hidden="true" className="text-warning mt-0.5 size-5 shrink-0" />
      <div className="min-w-0 flex-1 space-y-1">
        <h2 className="font-semibold">{title}</h2>
        {children ? <div className="text-sm">{children}</div> : null}
        {action ? <div className="pt-1">{action}</div> : null}
      </div>
    </section>
  );
}
