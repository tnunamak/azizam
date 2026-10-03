"use client";

import * as RadioGroupPrimitive from "@radix-ui/react-radio-group";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";

export type RadioCardsProps = ComponentPropsWithoutRef<
  typeof RadioGroupPrimitive.Root
>;

/** Card-style radio group (e.g. "Shared app" vs "Personal app" in the
 * create-app dialog). Compose with <RadioCard>. */
export function RadioCards({ className, ...props }: RadioCardsProps) {
  return (
    <RadioGroupPrimitive.Root
      className={cn("grid gap-2 sm:grid-cols-2", className)}
      {...props}
    />
  );
}

export interface RadioCardProps extends Omit<
  ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item>,
  "title"
> {
  title: ReactNode;
  description?: ReactNode;
}

export function RadioCard({
  title,
  description,
  className,
  ...props
}: RadioCardProps) {
  return (
    <RadioGroupPrimitive.Item
      className={cn(
        "group rounded-vf-md border-vf-border bg-vf-surface-1 border p-4 text-left transition-colors",
        "hover:border-vf-border-strong",
        "focus-visible:ring-vf-accent/60 focus-visible:ring-offset-vf-bg focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none",
        "data-[state=checked]:border-vf-accent data-[state=checked]:bg-vf-accent/10",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <span className="flex items-center gap-2">
        <span className="rounded-vf-pill border-vf-border-strong group-data-[state=checked]:border-vf-accent flex size-4 shrink-0 items-center justify-center border transition-colors">
          <RadioGroupPrimitive.Indicator className="rounded-vf-pill bg-vf-accent size-2" />
        </span>
        <span className="text-vf-body text-vf-text font-medium">{title}</span>
      </span>
      {description && (
        <span className="text-vf-small text-vf-muted mt-1 block pl-6">
          {description}
        </span>
      )}
    </RadioGroupPrimitive.Item>
  );
}
