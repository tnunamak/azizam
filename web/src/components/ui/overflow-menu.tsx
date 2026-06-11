"use client";

import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";
import { DotsIcon } from "./icons";
import { IconButton } from "./icon-button";

export interface OverflowMenuProps {
  /** Accessible name for the trigger, e.g. "Actions for brother-laptop". */
  label?: string;
  align?: "start" | "end";
  children: ReactNode;
}

/** The one row-actions affordance (presentation law #6: destructive actions
 * are demoted into a ⋯ menu, never a column of red buttons). */
export function OverflowMenu({
  label = "More actions",
  align = "end",
  children,
}: OverflowMenuProps) {
  return (
    <DropdownMenuPrimitive.Root>
      <DropdownMenuPrimitive.Trigger asChild>
        <IconButton aria-label={label} variant="ghost" size="sm">
          <DotsIcon className="size-4" />
        </IconButton>
      </DropdownMenuPrimitive.Trigger>
      <DropdownMenuPrimitive.Portal>
        <DropdownMenuPrimitive.Content
          align={align}
          sideOffset={4}
          className="rounded-vf-md border-vf-border bg-vf-surface-2 shadow-vf-caustic-2 z-50 min-w-44 border p-1"
        >
          {children}
        </DropdownMenuPrimitive.Content>
      </DropdownMenuPrimitive.Portal>
    </DropdownMenuPrimitive.Root>
  );
}

export interface OverflowMenuItemProps extends ComponentPropsWithoutRef<
  typeof DropdownMenuPrimitive.Item
> {
  /** Destructive styling; keep these items last, below a separator. */
  danger?: boolean;
}

export function OverflowMenuItem({
  danger = false,
  className,
  ...props
}: OverflowMenuItemProps) {
  return (
    <DropdownMenuPrimitive.Item
      className={cn(
        "rounded-vf-sm text-vf-small flex cursor-pointer items-center gap-2 px-3 py-2 outline-none select-none",
        "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
        danger
          ? "text-vf-bad data-[highlighted]:bg-vf-bad/15"
          : "text-vf-text data-[highlighted]:bg-vf-surface-3",
        className,
      )}
      {...props}
    />
  );
}

export function OverflowMenuSeparator({ className }: { className?: string }) {
  return (
    <DropdownMenuPrimitive.Separator
      className={cn("bg-vf-border my-1 h-px", className)}
    />
  );
}
