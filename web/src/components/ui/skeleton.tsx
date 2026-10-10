import { cn } from "@/lib/cn";

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("bg-muted rounded-control animate-pulse", className)}
      {...props}
    />
  );
}

/**
 * Loading rows at the exact row heights (DESIGN "Empty, loading and error"):
 * 40 for one line, 60 for two (48 and 64 on a phone), hairlines between them,
 * so the list does not jump when it loads.
 */
function SkeletonRows({
  count = 3,
  lines = 1,
  className,
}: {
  count?: number;
  lines?: 1 | 2;
  className?: string;
}) {
  return (
    <div
      data-slot="skeleton-rows"
      aria-hidden="true"
      className={cn("divide-y", className)}
    >
      {Array.from({ length: count }, (_, index) => (
        <div
          key={index}
          data-slot="skeleton-row"
          className={cn(
            "flex flex-col justify-center gap-2 px-4",
            lines === 2
              ? "h-(--row-2) max-sm:h-(--row-phone-2)"
              : "h-(--row-1) max-sm:h-(--row-phone-1)",
          )}
        >
          <Skeleton className="h-3.5 w-2/5" />
          {lines === 2 ? <Skeleton className="h-3 w-1/4" /> : null}
        </div>
      ))}
    </div>
  );
}

export { Skeleton, SkeletonRows };
