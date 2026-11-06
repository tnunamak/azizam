"use client";

import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";
import { useCompactTouchTargetClass } from "./compact-touch-targets";
import { Spinner } from "./spinner";

export type IconButtonVariant = "ghost" | "secondary" | "danger";
export type IconButtonSize = "sm" | "md";

const VARIANT_CLASSES: Record<IconButtonVariant, string> = {
  ghost:
    "text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-primary/60",
  secondary:
    "border border-border bg-muted text-muted-foreground hover:border-input hover:text-foreground focus-visible:ring-primary/60",
  danger: "text-danger hover:bg-danger/15 focus-visible:ring-danger/60",
};

const SIZE_CLASSES: Record<IconButtonSize, string> = {
  sm: "size-8",
  md: "size-9",
};

export interface IconButtonProps extends ComponentPropsWithoutRef<"button"> {
  /** Required: icon-only buttons must name their action for screen readers. */
  "aria-label": string;
  variant?: IconButtonVariant;
  size?: IconButtonSize;
  loading?: boolean;
}

export function IconButton({
  variant = "ghost",
  size = "md",
  loading = false,
  className,
  children,
  disabled,
  type,
  ...props
}: IconButtonProps) {
  const compactTouchTargetClass = useCompactTouchTargetClass();
  return (
    <button
      type={type ?? "button"}
      className={cn(
        "rounded-md focus-visible:ring-offset-background inline-flex shrink-0 items-center justify-center transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 max-sm:min-h-11 max-sm:min-w-11",
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        compactTouchTargetClass,
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Spinner className="size-4" /> : children}
    </button>
  );
}
