"use client";

import * as React from "react";
import { cn } from "@/lib/cn";
import { Switch as SwitchPrimitive } from "radix-ui";

/** A 32 × 20 track (Linear 29 × 20); a 44 × 44 hit area below 640 px. */
function Switch({
  className,
  size = "default",
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root> & {
  size?: "sm" | "default";
}) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      data-size={size}
      className={cn(
        "peer group/switch focus-visible:border-ring focus-visible:ring-ring/50 inline-flex shrink-0 items-center justify-center rounded-full border border-transparent outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50 data-[size=default]:h-5 data-[size=default]:w-8 data-[size=sm]:h-3.5 data-[size=sm]:w-6 max-sm:min-h-11 max-sm:min-w-11",
        className,
      )}
      {...props}
    >
      <span
        data-slot="switch-track"
        aria-hidden="true"
        className="group-data-[state=checked]/switch:bg-primary group-data-[state=unchecked]/switch:bg-input pointer-events-none inline-flex shrink-0 items-center rounded-full border border-transparent transition-colors group-data-[size=default]/switch:h-5 group-data-[size=default]/switch:w-8 group-data-[size=sm]/switch:h-3.5 group-data-[size=sm]/switch:w-6"
      >
        <SwitchPrimitive.Thumb
          data-slot="switch-thumb"
          className="bg-background pointer-events-none block rounded-full ring-0 transition-transform group-data-[size=default]/switch:size-4 group-data-[size=sm]/switch:size-3 data-[state=checked]:translate-x-[calc(100%-2px)] data-[state=unchecked]:translate-x-0"
        />
      </span>
    </SwitchPrimitive.Root>
  );
}

export { Switch };
