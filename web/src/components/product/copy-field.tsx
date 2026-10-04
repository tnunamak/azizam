"use client";
import { useId, useLayoutEffect, useRef, useState } from "react";
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
  label = "Gateway address",
  disabled = false,
  loading = false,
  error,
}: CopyFieldProps) {
  const id = useId();
  const [feedback, setFeedback] = useState<{ copied: boolean } | null>(null);
  const attempt = useRef(0);
  useLayoutEffect(() => {
    attempt.current += 1;
    setFeedback(null);
    return () => {
      attempt.current += 1;
    };
  }, [value, label]);
  const copied = feedback?.copied;
  const copyError = feedback && !feedback.copied;
  async function copy() {
    const request = ++attempt.current;
    setFeedback(null);
    try {
      await navigator.clipboard.writeText(value);
      if (attempt.current === request) setFeedback({ copied: true });
    } catch {
      if (attempt.current === request) setFeedback({ copied: false });
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
        {copied ? `${label} copied to clipboard.` : ""}
      </span>
      {(error || copyError) && (
        <p id={`${id}-error`} role="alert" className="text-destructive text-sm">
          {error || "Copy failed. Select the value and copy it manually."}
        </p>
      )}
    </div>
  );
}
