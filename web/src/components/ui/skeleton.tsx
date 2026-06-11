import { cn } from "@/lib/cn";

/** Loading placeholder bar. Size it with layout utilities (h-4 w-32 …). */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn("rounded-vf-sm bg-vf-surface-2 animate-pulse", className)}
      aria-hidden="true"
    />
  );
}
