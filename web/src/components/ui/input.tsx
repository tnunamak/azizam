import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * `md` (40, `text-body`) in forms, the default; `sm` (32, `text-meta`) in
 * toolbars and rows, beside 32 px buttons. Both are 44 below 640 px. The edge stays `--input` (3:1,
 * DESIGN "Field edge").
 */
function Input({
  className,
  type,
  size = "md",
  ...props
}: Omit<React.ComponentProps<"input">, "size"> & { size?: "sm" | "md" }) {
  // The size owns desktop height; callers may only adjust phone heights.
  const callerClasses = className
    ?.split(/\s+/)
    .filter((token) => token.startsWith("max-sm:") || !/(?:^|:)!?h-/.test(token))
    .join(" ");
  return (
    <input
      type={type}
      data-slot="input"
      data-size={size}
      className={cn(
        "border-input bg-card file:text-foreground placeholder:text-muted-foreground w-full min-w-0 rounded-control border px-3 py-1 transition-colors file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-meta file:font-medium disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 max-sm:h-11",
        size === "sm" ? "sm:h-8" : "sm:h-10",
        size === "sm" ? "text-meta" : "text-body",
        "focus-visible:border-ring",
        "aria-invalid:border-destructive",
        callerClasses,
      )}
      {...props}
    />
  );
}

export { Input };
