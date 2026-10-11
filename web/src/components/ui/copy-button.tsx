"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";
import { Button } from "./button";

// Preserve CopyButton’s public props while using the modern primitive.
type ButtonSize = "sm" | "md" | "lg";
type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
import { CheckIcon, CopyIcon } from "./icons";

/** Copy text to the clipboard. Falls back to a hidden textarea when the
 * async Clipboard API is unavailable (e.g. plain-http LAN deployments). */
export async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  let textarea: HTMLTextAreaElement | undefined;
  try {
    textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.className = "sr-only";
    document.body.appendChild(textarea);
    textarea.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea?.remove();
  }
}

/** Shared "flash a check for 2s after copying" state. */
export function useCopied(): {
  copied: boolean;
  failed: boolean;
  copy: (value: string) => Promise<void>;
} {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const copy = useCallback(async (value: string) => {
    const ok = await copyText(value);
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    setCopied(ok);
    setFailed(!ok);
    if (ok) timer.current = setTimeout(() => setCopied(false), 2000);
  }, []);

  return { copied, failed, copy };
}

export interface CopyButtonProps {
  /** Text placed on the clipboard. Never logged. */
  value: string;
  /** Visible label; icon-only when omitted. */
  children?: ReactNode;
  size?: ButtonSize;
  variant?: ButtonVariant;
  className?: string;
  "aria-label"?: string;
}

export function CopyButton({
  value,
  children,
  size = "sm",
  variant = "secondary",
  className,
  "aria-label": ariaLabel,
}: CopyButtonProps) {
  const { copied, failed, copy } = useCopied();
  return (
    <Button
      type="button"
      size={size}
      variant={
        variant === "primary"
          ? "default"
          : variant === "danger"
            ? "destructive"
            : variant === "secondary"
              ? "outline"
              : "ghost"
      }
      className={cn(children ? undefined : "px-2", className)}
      aria-label={ariaLabel ?? (children ? undefined : "Copy")}
      onClick={() => void copy(value)}
    >
      {copied ? (
        <CheckIcon className="text-success size-4" />
      ) : (
        <CopyIcon className="size-4" />
      )}
      {children}
      <span role="status" aria-live="polite" className="sr-only">
        {failed
          ? "Couldn't copy. Select the text and copy it yourself."
          : copied
            ? "Copied to clipboard"
            : ""}
      </span>
    </Button>
  );
}
