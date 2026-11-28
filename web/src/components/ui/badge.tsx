import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";
import { Slot } from "radix-ui";
import type { CapabilityStateKey } from "@/lib/capability-state";

/**
 * DESIGN "Status display": 20 tall, radius 4, padding 0 6, `text-badge`, a
 * 14 icon. `variant="state"` draws a state in its tint and ink; pair it with
 * the state's icon and word (StateBadge in status-ui does both).
 */
const badgeVariants = cva(
  "rounded-badge text-badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden border border-transparent px-1.5 whitespace-nowrap transition-[color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 [&>svg]:pointer-events-none [&>svg]:size-3.5",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a&]:hover:bg-primary/90",
        secondary:
          "bg-secondary text-secondary-foreground [a&]:hover:bg-secondary/90",
        destructive:
          "bg-destructive text-destructive-foreground focus-visible:ring-destructive/20   [a&]:hover:bg-destructive/90",
        outline:
          "border-border text-foreground [a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        ghost: "[a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        link: "text-link underline-offset-4 [a&]:hover:underline",
        state: "",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

/** Tint and ink per state; every pair is 4.5:1 or better (state_palette.py). */
const stateBadgeTone: Record<CapabilityStateKey, string> = {
  ready: "bg-success-tint text-success",
  busy: "bg-info-tint text-info",
  degraded: "bg-warning-tint text-warning",
  down: "bg-danger-tint text-danger",
  blocked: "bg-blocked-tint text-blocked",
  unknown: "bg-muted text-subtle-foreground",
  off: "bg-muted text-subtle-foreground",
};

function Badge({
  className,
  variant = "default",
  state,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & {
    asChild?: boolean;
    /** With `variant="state"`: which state's tint and ink to use. */
    state?: CapabilityStateKey;
  }) {
  const Comp = asChild ? Slot.Root : "span";

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      data-state-key={variant === "state" ? (state ?? "unknown") : undefined}
      className={cn(
        badgeVariants({ variant }),
        variant === "state" && stateBadgeTone[state ?? "unknown"],
        className,
      )}
      {...props}
    />
  );
}

export { Badge, badgeVariants, stateBadgeTone };
