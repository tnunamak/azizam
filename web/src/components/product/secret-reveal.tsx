"use client";
import { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyField } from "./copy-field";

export interface SecretRevealProps {
  secret: string | null;
  label?: string;
  onSaved?: () => void;
}
/** For newly issued secrets only: confirmation permanently removes the value from this view. */
export function SecretReveal({
  secret,
  label = "Your key",
  onSaved,
}: SecretRevealProps) {
  const [saved, setSaved] = useState(false);
  if (saved || !secret)
    return (
      <div
        role="status"
        className="text-muted-foreground flex items-center gap-2 text-sm"
      >
        <ShieldCheck className="size-4" aria-hidden="true" />
        {saved
          ? "Key hidden. It won’t be shown again."
          : "This key is no longer available. Create a new key if you need one."}
      </div>
    );
  return (
    <div className="flex flex-col gap-4">
      <p className="text-muted-foreground text-sm">
        Copy this key now. You can’t view it again after closing this step.
      </p>
      <CopyField value={secret} label={label} />
      <Button
        type="button"
        onClick={() => {
          setSaved(true);
          onSaved?.();
        }}
      >
        I’ve saved it
      </Button>
    </div>
  );
}
