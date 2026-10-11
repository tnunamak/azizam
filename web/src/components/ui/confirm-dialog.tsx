"use client";

import { useRef, useState } from "react";
import type { ReactNode } from "react";

import { Button } from "./button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog";
import { Field } from "./field";
import { Input } from "./input";
import { Spinner } from "./spinner";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  /** e.g. "Disable app". Defaults to "Confirm". */
  confirmLabel?: string;
  cancelLabel?: string;
  /** Destructive styling (default). Set false for non-destructive confirms. */
  danger?: boolean;
  /** Typed-name mode for high-impact deletes (presentation law #5): the
   * confirm button stays disabled until the user types this exact name. */
  typedName?: string;
  /** Stable invoker when opened from a menu whose items unmount. */
  returnFocus?: HTMLElement | null;
  /** Runs on confirm; the dialog shows a loading state while a returned
   * promise is pending and closes only on success. The caller owns error
   * presentation; rejected mutations leave the dialog open. */
  onConfirm: () => void | Promise<void>;
}

/** Replaces window.confirm()/alert() everywhere (presentation law #5). */
export function ConfirmDialog({
  open,
  onOpenChange,
  ...rest
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open so typed/pending state starts fresh each
          time — no state-syncing effects. */}
      {open && <ConfirmDialogContent onOpenChange={onOpenChange} {...rest} />}
    </Dialog>
  );
}

function ConfirmDialogContent({
  onOpenChange,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = true,
  typedName,
  returnFocus: invoker,
  onConfirm,
}: Omit<ConfirmDialogProps, "open">) {
  const returnFocus = useRef<HTMLElement | null>(
    typeof document === "undefined"
      ? null
      : (document.activeElement as HTMLElement),
  );
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);

  const nameMismatch = typedName !== undefined && typed !== typedName;

  async function handleConfirm() {
    if (pending || nameMismatch) return;
    setPending(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch {
      // Route mutations report the failure. Consume it here so the event
      // handler never creates an unhandled rejection while retaining target.
    } finally {
      setPending(false);
    }
  }

  // No dismissal mid-flight: swallow escape/outside-click while pending.
  const blockWhilePending = (event: Event) => {
    if (pending) event.preventDefault();
  };

  return (
    <DialogContent
      showCloseButton={false}
      className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden sm:max-w-md"
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        (invoker ?? returnFocus.current)?.focus();
      }}
      onEscapeKeyDown={blockWhilePending}
      onPointerDownOutside={blockWhilePending}
      onInteractOutside={blockWhilePending}
    >
      <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
        <DialogTitle>{title}</DialogTitle>
        {description && <DialogDescription>{description}</DialogDescription>}
      </DialogHeader>
      {typedName !== undefined && (
        <div
          data-slot="dialog-body"
          className="-mx-6 mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)] px-6"
        >
          <Field
            label={
              <>
                Type{" "}
                <span className="text-foreground font-mono text-xs">
                  {typedName}
                </span>{" "}
                to confirm
              </>
            }
          >
            <Input
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              autoFocus
              disabled={pending}
            />
          </Field>
        </div>
      )}
      <DialogFooter className="mt-6 shrink-0">
        <Button
          size="md"
          type="button"
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={pending}
        >
          {cancelLabel}
        </Button>
        <Button
          size="md"
          type="button"
          variant={danger ? "destructive" : "default"}
          aria-busy={pending || undefined}
          disabled={pending || nameMismatch}
          onClick={() => void handleConfirm()}
        >
          {pending && <Spinner className="size-4" />}
          {confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
