"use client";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { administratorsSentence, type AdministratorsOnRecord } from "@/lib/account-api";

/** A keep that waits for the owner to confirm the administrators on record. `id` is the key's or address's id. */
export interface KeepConfirmation {
  id: string;
  what: "key" | "address";
  administrators: AdministratorsOnRecord;
  /** Why the last confirmed keep failed. The dialog stays open to show it. */
  error?: string;
}

/** The sentence the owner reads before keeping a binding that administrators on record could also have made. */
export function keepConfirmBody(administrators: AdministratorsOnRecord): string {
  const named = administratorsSentence(administrators);
  const record = named ? ` Administrators at the time could also have made it: ${named}.` : "";
  return `Azizam did not record who made it.${record} Keep it only if you made it or trust them.`;
}

/** Asks the owner to confirm the administrators before a keep. A rejected `onConfirm` leaves the dialog open. */
export function KeepConfirmDialog({
  confirmation,
  onClose,
  onConfirm,
  fallbackFocus,
}: {
  confirmation: KeepConfirmation | null;
  onClose: () => void;
  onConfirm: () => Promise<void>;
  /** Where focus goes when the binding is kept, because its Keep button has gone. */
  fallbackFocus?: () => (HTMLElement | null)[];
}) {
  const what = confirmation?.what ?? "key";
  return (
    <ConfirmDialog
      open={confirmation !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`Keep this ${what}?`}
      description={
        confirmation && (
          <>
            <span className="block">{keepConfirmBody(confirmation.administrators)}</span>
            {confirmation.error ? (
              <span role="alert" className="text-destructive block">
                {confirmation.error}
              </span>
            ) : null}
          </>
        )
      }
      confirmLabel={`Keep ${what}`}
      cancelLabel="Cancel"
      danger={false}
      fallbackFocus={fallbackFocus}
      onConfirm={onConfirm}
    />
  );
}
