"use client";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { endSession } from "@/lib/session-end";
import { unsavedWorkLines, type UnsavedWork } from "@/lib/unsaved-work";

/** A person asked to leave while work would be lost. `opener` gets focus back on Keep working or Escape. */
export interface LeaveRequest {
  end: "switch" | "sign-out";
  lost: readonly UnsavedWork[];
  opener: HTMLElement | null;
}

/** The confirm before Switch person or Sign out ends a session with work in it (DESIGN.md 4.2, S2). */
export function SwitchConfirm({ request, onClose }: { request: LeaveRequest | null; onClose: () => void }) {
  if (!request) return null;
  const switching = request.end === "switch";
  return (
    <ConfirmDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={switching ? "Switch person?" : "Sign out?"}
      description={
        <>
          <span className="block">
            {switching ? "Switching signs you out. This will be lost:" : "This will be lost:"}
          </span>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {unsavedWorkLines(request.lost).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      }
      confirmLabel={switching ? "Discard and switch" : "Discard and sign out"}
      cancelLabel="Keep working"
      cancelVariant="outline"
      danger={false}
      returnFocus={request.opener}
      onConfirm={() => {
        void endSession(request.end);
      }}
    />
  );
}
