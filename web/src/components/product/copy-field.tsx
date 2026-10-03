"use client";
import { useId, useState } from "react";
import { Check, Copy, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface CopyFieldProps {
  value: string;
  label?: string;
  disabled?: boolean;
  loading?: boolean;
  error?: string;
}
export function CopyField({
  value,
  label = "Base URL",
  disabled = false,
  loading = false,
  error,
}: CopyFieldProps) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setCopyError(false);
    } catch {
      setCopied(false);
      setCopyError(true);
    }
  }
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex min-w-0 gap-2">
        <Input
          id={id}
          value={loading ? "" : value}
          readOnly
          disabled={disabled || loading}
          aria-invalid={!!error}
          aria-describedby={error || copyError ? `${id}-error` : undefined}
          className="min-w-0 font-mono"
        />
        <Button
          type="button"
          variant="outline"
          size="icon"
          disabled={disabled || loading || !value}
          onClick={copy}
          aria-label={`Copy ${label.toLowerCase()}`}
        >
          {loading ? (
            <Loader2 className="animate-spin" />
          ) : copied ? (
            <Check />
          ) : (
            <Copy />
          )}
        </Button>
      </div>
      <span className="sr-only" role="status">
        {copied ? "Copied to clipboard" : ""}
      </span>
      {(error || copyError) && (
        <p id={`${id}-error`} className="text-destructive text-sm">
          {error || "Copy failed. Select the value and copy it manually."}
        </p>
      )}
    </div>
  );
}
