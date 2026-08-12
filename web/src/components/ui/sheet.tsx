"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";
import { IconButton } from "./icon-button";
import { XIcon } from "./icons";

/** Side panel for detail views (app keys, receipts). Right-hand panel on
 * desktop, bottom sheet on mobile — same component, CSS-only switch. */
export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

export type SheetContentProps = ComponentPropsWithoutRef<
  typeof DialogPrimitive.Content
>;

export function SheetContent({
  className,
  children,
  ...props
}: SheetContentProps) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        className="bg-vf-bg/70 fixed inset-0 z-50 backdrop-blur-sm"
        data-testid="sheet-overlay"
      />
      <DialogPrimitive.Content
        className={cn(
          "border-vf-border bg-vf-surface-1 shadow-vf-caustic-2 fixed z-50 flex flex-col gap-4 overflow-y-auto p-6 focus:outline-none",
          "max-md:rounded-t-vf-lg max-md:inset-x-0 max-md:bottom-0 max-md:max-h-[85dvh] max-md:border-t",
          "md:inset-y-0 md:right-0 md:w-full md:max-w-md md:border-l",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close asChild>
          <IconButton
            aria-label="Close"
            variant="ghost"
            size="sm"
            className="absolute top-4 right-4"
          >
            <XIcon className="size-4" />
          </IconButton>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function SheetHeader({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5 pr-8", className)}>
      {children}
    </div>
  );
}

export function SheetTitle({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn("text-vf-heading text-vf-text", className)}
      {...props}
    />
  );
}

export function SheetDescription({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn("text-vf-small text-vf-muted", className)}
      {...props}
    />
  );
}

export function SheetFooter({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "mt-auto flex flex-col-reverse gap-2 pt-4 sm:flex-row sm:justify-end",
        className,
      )}
    >
      {children}
    </div>
  );
}
