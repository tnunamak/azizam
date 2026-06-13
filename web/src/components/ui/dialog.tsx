"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import React from "react";

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
            "rounded-vf-lg border-vf-border bg-vf-surface-1 shadow-vf-caustic-2 relative flex w-full max-w-lg flex-col border focus:outline-none",
            "max-h-[85dvh] overflow-hidden",
            className,
          )}
          {...props}
        >
          {/* Inner wrapper applies horizontal padding so the scrollbar hugs
              the dialog border. Header and footer are padded here; the
              scrollable body extends edge-to-edge so the scrollbar sits flush
              against the right border with no mid-gutter float. */}
          <div className="flex min-h-0 flex-1 flex-col px-6 pt-6">
            {children}
          </div>
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

/** Wrap a `<form>` inside DialogContent so it participates in the flex-column
 * height layout (header/footer pinned, body scrolls). Required whenever a
 * dialog has a form that wraps DialogHeader + DialogBody + DialogFooter. */
export function DialogForm({
  className,
  children,
  ...props
}: React.ComponentPropsWithoutRef<"form">) {
  return (
    <form
      className={cn("flex min-h-0 flex-1 flex-col", className)}
      {...props}
    >
      {children}
    </form>
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
    <div className={cn("shrink-0 flex flex-col gap-1.5 pr-8", className)}>
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
    /* -mx-6 pulls the scroll container to the dialog edge so the native
       scrollbar hugs the border. px-6 restores the gutter for the content
       so it aligns with the header/footer. The fade masks clean up the
       clip edge under the pinned header and footer. */
    <div
      className={cn(
        "mt-4 min-h-0 flex-1 overflow-y-auto flex flex-col gap-4",
        "-mx-6 px-6",
        "[mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)]",
        className,
      )}
    >
      {children}
    </div>
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
        "shrink-0 mt-6 pb-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className,
      )}
    >
      {children}
    </div>
  );
}
