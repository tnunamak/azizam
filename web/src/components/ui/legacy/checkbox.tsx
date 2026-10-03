"use client";

import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";
import { CheckIcon } from "../icons";
import { useFieldContext } from "../field";

export interface CheckboxProps extends ComponentPropsWithoutRef<
  typeof CheckboxPrimitive.Root
> {
  /** Inline label; clicking it toggles the box. */
  children?: ReactNode;
}

export function Checkbox({ className, children, ...props }: CheckboxProps) {
  const field = useFieldContext();

  const box = (
    <CheckboxPrimitive.Root
      id={props.id ?? (children ? undefined : field?.id)}
      aria-describedby={field?.describedBy}
      className={cn(
        "rounded-vf-sm border-vf-border bg-vf-surface-1 flex size-5 shrink-0 items-center justify-center border transition-colors",
        "hover:border-vf-border-strong",
        "focus-visible:ring-vf-accent/60 focus-visible:ring-offset-vf-bg focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none",
        "data-[state=checked]:border-vf-accent data-[state=checked]:bg-vf-accent",
        "disabled:cursor-not-allowed disabled:opacity-50",
        !children && className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="text-vf-accent-ink">
        <CheckIcon className="size-3.5" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );

  if (!children) return box;

  return (
    <label
      className={cn(
        "text-vf-body text-vf-text flex cursor-pointer items-start gap-2 has-[[disabled]]:cursor-not-allowed has-[[disabled]]:opacity-60",
        className,
      )}
    >
      {box}
      <span className="text-vf-small pt-0.5">{children}</span>
    </label>
  );
}
