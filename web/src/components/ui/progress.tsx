"use client";

import * as React from "react";
import { cn } from "@/lib/cn";
import { Progress as ProgressPrimitive } from "radix-ui";

function Progress({
  className,
  value,
  max = 100,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root>) {
  const maximum = Number.isFinite(max) && max > 0 ? max : 100;
  const current = typeof value === "number" && Number.isFinite(value)
    ? Math.min(maximum, Math.max(0, value))
    : null;
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      value={current}
      max={maximum}
      className={cn(
        "bg-primary/20 relative h-2 w-full overflow-hidden rounded-full",
        className,
      )}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn("bg-primary h-full transition-all", current === null ? "w-1/3 motion-safe:animate-pulse" : "w-full")}
        style={current === null ? undefined : { transform: `translateX(-${100 - current / maximum * 100}%)` }}
      />
    </ProgressPrimitive.Root>
  );
}

export { Progress };
