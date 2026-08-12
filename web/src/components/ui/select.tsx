"use client";

import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";
import { useCompactTouchTargetClass } from "./compact-touch-targets";
import { ChevronDownIcon } from "./icons";
import { useFieldContext } from "./field";

export type SelectProps = ComponentPropsWithoutRef<"select">;

/** Styled native select — right control for the short option lists on these
 * surfaces (profiles, owners); the platform picker beats a custom one on
 * mobile. Pass <option> children. */
export function Select({ className, children, ...props }: SelectProps) {
  const compactTouchTargetClass = useCompactTouchTargetClass();
  const field = useFieldContext();
  const invalid = field?.invalid || props["aria-invalid"] === true;

  return (
    <span className={cn("relative inline-flex w-full", className)}>
      <select
        id={props.id ?? field?.id}
        aria-describedby={props["aria-describedby"] ?? field?.describedBy}
        aria-invalid={invalid || undefined}
        className={cn(
          "rounded-vf-md bg-vf-surface-1 text-vf-body text-vf-text h-9 w-full appearance-none border pr-8 pl-3 transition-colors",
          compactTouchTargetClass,
          "focus:border-vf-accent focus:ring-vf-accent/30 focus:ring-2 focus:outline-none",
          "disabled:cursor-not-allowed disabled:opacity-50",
          invalid
            ? "border-vf-bad focus:border-vf-bad focus:ring-vf-bad/30"
            : "border-vf-border hover:border-vf-border-strong",
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDownIcon className="text-vf-subtle pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2" />
    </span>
  );
}
