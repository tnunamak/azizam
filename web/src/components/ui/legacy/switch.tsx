"use client";

import * as SwitchPrimitive from "@radix-ui/react-switch";
import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";
import { useCompactTouchTargetClass } from "../compact-touch-targets";
import { useFieldContext } from "../field";

export type SwitchProps = ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>;

export function Switch({ className, ...props }: SwitchProps) {
  const compactTouchTargetClass = useCompactTouchTargetClass();
  const field = useFieldContext();

  return (
    <SwitchPrimitive.Root
      id={props.id ?? field?.id}
      aria-describedby={field?.describedBy}
      className={cn(
        "rounded-vf-pill border-vf-border bg-vf-surface-3 inline-flex h-5 w-9 shrink-0 items-center border transition-colors",
        compactTouchTargetClass,
        "hover:border-vf-border-strong",
        "focus-visible:ring-vf-accent/60 focus-visible:ring-offset-vf-bg focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none",
        "data-[state=checked]:border-vf-accent data-[state=checked]:bg-vf-accent",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="rounded-vf-pill bg-vf-text data-[state=checked]:bg-vf-accent-ink block size-4 translate-x-0.5 transition-transform data-[state=checked]:translate-x-4" />
    </SwitchPrimitive.Root>
  );
}
