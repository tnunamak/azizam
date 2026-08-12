"use client";

import { useState } from "react";
import type { ReactNode } from "react";

import { Button } from "./button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog";
import { Field } from "./field";
import { Input } from "./input";

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
  /** Runs on confirm; the dialog shows a loading state while a returned
   * promise is pending and closes only on success. The caller owns error
   * reporting; failures leave the confirmation open for retry. */
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
  onConfirm,
}: Omit<ConfirmDialogProps, "open">) {
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);

  const nameMismatch = typedName !== undefined && typed !== typedName;

  async function handleConfirm() {
    setPending(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch {
      // The caller reports mutation failures. Keep the target and typed value
      // in place so the operator can review and retry the destructive action.
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
      hideClose
      className="max-w-md"
      onEscapeKeyDown={blockWhilePending}
      onPointerDownOutside={blockWhilePending}
      onInteractOutside={blockWhilePending}
    >
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        {description && <DialogDescription>{description}</DialogDescription>}
      </DialogHeader>
      {typedName !== undefined && (
        <DialogBody>
          <Field
            label={
              <>
                Type{" "}
                <span className="text-vf-micro text-vf-text font-mono">
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
        </DialogBody>
      )}
      <DialogFooter>
        <Button
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={pending}
        >
          {cancelLabel}
        </Button>
        <Button
          variant={danger ? "danger" : "primary"}
          loading={pending}
          disabled={nameMismatch}
          onClick={() => void handleConfirm()}
        >
          {confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
