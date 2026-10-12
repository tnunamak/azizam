"use client";

import { Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCopied } from "@/components/ui/copy-button";

export function ReceiptLink({ id }: { id: string }) {
  const { copied, copy } = useCopied();
  const shortId = id.length > 12 ? `${id.slice(0, 8)}…` : id;
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-2">
      <a
        className="inline-flex min-h-11 items-center font-mono text-xs whitespace-nowrap underline underline-offset-4"
        href={`/usage/?receipt=${encodeURIComponent(id)}`}
        aria-label={`Open receipt ${shortId}`}
        title={id}
      >
        {shortId}
      </a>
      <Button
        variant="ghost"
        size="icon"
        className="size-11 shrink-0"
        aria-label="Copy receipt ID"
        onClick={() => void copy(id)}
      >
        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </Button>
      <span className="sr-only" aria-live="polite">
        {copied ? "Receipt ID copied" : ""}
      </span>
    </span>
  );
}
