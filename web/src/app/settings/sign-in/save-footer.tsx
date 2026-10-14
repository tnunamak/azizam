"use client";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * Sticks to the bottom of the screen while the Sign-in section is in view. On
 * a phone it sits above the tab bar and sums up the changes ("2 changes ·
 * Review"); the caption always says why Save is off, or what it will do.
 */
export function SaveFooter({
  caption,
  need,
  canSave,
  saving,
  saveLabel,
  showDiscard,
  later,
  error,
  onSave,
  onLater,
  onDiscard,
}: {
  caption: ReactNode;
  /** The caption is a reason, not a description: draw it in the warning colour. */
  need: boolean;
  canSave: boolean;
  saving: boolean;
  saveLabel: string;
  showDiscard: boolean;
  /** "Save for later" — shown while the provider is off. */
  later: { enabled: boolean } | null;
  error: ReactNode;
  onSave: () => void;
  onLater: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="sticky bottom-16 z-20 sm:bottom-3">
      {error && (
        <div
          role="alert"
          className="bg-danger-tint text-danger rounded-control text-meta mb-2 px-3 py-2"
        >
          {error}
        </div>
      )}
      <div className="bg-card rounded-panel shadow-float flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5 border px-4 py-3">
        <p
          role="status"
          data-slot="sign-in-caption"
          className={cn(
            "text-meta min-w-0 flex-1 basis-72",
            need ? "text-warning font-medium" : "text-muted-foreground",
          )}
        >
          {caption}
        </p>
        <div className="flex flex-wrap gap-2 max-sm:w-full max-sm:[&>*]:flex-1">
          {showDiscard && (
            <Button variant="ghost" size="md" onClick={onDiscard}>
              Discard changes
            </Button>
          )}
          {later && (
            <Button
              variant="outline"
              size="md"
              disabled={!later.enabled || saving}
              onClick={onLater}
            >
              Save for later
            </Button>
          )}
          <Button size="md" disabled={!canSave || saving} onClick={onSave}>
            {saving ? "Saving…" : saveLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
