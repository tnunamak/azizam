"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import type { ComponentPropsWithoutRef, ReactNode } from "react";

import { cn } from "@/lib/cn";
import { IconButton } from "./icon-button";
import { XIcon } from "./icons";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export interface DialogContentProps extends ComponentPropsWithoutRef<
  typeof DialogPrimitive.Content
> {
  /** Hide the corner ✕ (e.g. reveal-once flows that force an explicit
   * "I saved it" action). */
  hideClose?: boolean;
}

export function DialogContent({
  className,
  children,
  hideClose = false,
  ...props
}: DialogContentProps) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="bg-vf-bg/70 fixed inset-0 z-50 backdrop-blur-sm" />
      <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto p-4">
        <DialogPrimitive.Content
          className={cn(
            "rounded-vf-lg border-vf-border bg-vf-surface-1 shadow-vf-caustic-2 relative w-full max-w-lg border p-6 focus:outline-none",
            className,
          )}
          {...props}
        >
          {children}
          {!hideClose && (
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
          )}
        </DialogPrimitive.Content>
      </div>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({
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

export function DialogTitle({
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

export function DialogDescription({
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

export function DialogBody({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("mt-4 flex flex-col gap-4", className)}>{children}</div>
  );
}

export function DialogFooter({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className,
      )}
    >
      {children}
    </div>
  );
}
