"use client";

import { Slot } from "@radix-ui/react-slot";
import type { ComponentPropsWithoutRef } from "react";

import { cn } from "@/lib/cn";
import { Spinner } from "./spinner";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const BASE =
  "inline-flex select-none items-center justify-center gap-2 rounded-vf-md font-medium transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-vf-bg " +
  "disabled:pointer-events-none disabled:opacity-50";

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-vf-accent text-vf-accent-ink hover:bg-vf-accent/85 active:bg-vf-accent/75 focus-visible:ring-vf-accent/60",
  secondary:
    "border border-vf-border bg-vf-surface-2 text-vf-text hover:border-vf-border-strong hover:bg-vf-surface-3 focus-visible:ring-vf-accent/60",
  ghost:
    "text-vf-muted hover:bg-vf-surface-2 hover:text-vf-text focus-visible:ring-vf-accent/60",
  danger:
    "bg-vf-bad text-vf-accent-ink hover:bg-vf-bad/85 active:bg-vf-bad/75 focus-visible:ring-vf-bad/60",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-vf-small",
  md: "h-9 px-4 text-vf-small",
  lg: "h-11 px-5 text-vf-body",
};

export interface ButtonProps extends ComponentPropsWithoutRef<"button"> {
  /** Default is secondary — `primary` is the one accent action per surface
   * (steering doc: calm rows, one accent action). */
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Disables the button and shows a spinner next to the label. */
  loading?: boolean;
  /** Style the child element (e.g. a link) as a button. `loading` is
   * ignored with asChild — links don't have a pending state. */
  asChild?: boolean;
}

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  asChild = false,
  className,
  children,
  disabled,
  type,
  ...props
}: ButtonProps) {
  const classes = cn(
    BASE,
    VARIANT_CLASSES[variant],
    SIZE_CLASSES[size],
    className,
  );

  if (asChild) {
    return (
      <Slot className={classes} {...props}>
        {children}
      </Slot>
    );
  }

  return (
    <button
      type={type ?? "button"}
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <Spinner className="size-4" />}
      {children}
    </button>
  );
}
