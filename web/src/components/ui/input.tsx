"use client";

import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";
import { useCompactTouchTargetClass } from "./compact-touch-targets";
import { useFieldContext } from "./field";

export type InputProps = ComponentPropsWithoutRef<"input">;

export function Input({ className, ...props }: InputProps) {
  const compactTouchTargetClass = useCompactTouchTargetClass();
  const field = useFieldContext();
  const invalid = field?.invalid || props["aria-invalid"] === true;

  return (
    <input
      id={props.id ?? field?.id}
      aria-describedby={props["aria-describedby"] ?? field?.describedBy}
      aria-invalid={invalid || undefined}
      className={cn(
        "rounded-vf-md bg-vf-surface-1 text-vf-body text-vf-text h-9 w-full border px-3 transition-colors",
        compactTouchTargetClass,
        "placeholder:text-vf-subtle",
        "focus:border-vf-accent focus:ring-vf-accent/30 focus:ring-2 focus:outline-none",
        "disabled:cursor-not-allowed disabled:opacity-50",
        invalid
          ? "border-vf-bad focus:border-vf-bad focus:ring-vf-bad/30"
          : "border-vf-border hover:border-vf-border-strong",
        className,
      )}
      {...props}
    />
  );
}
