"use client";

import Link from "next/link";

import { cn } from "@/lib/cn";
import { CheckIcon } from "./icons";
import { useCopied } from "./copy-button";

export interface EntityRefProps {
  /** Human name — the primary content (presentation law #2). */
  name: string;
  /** Prefixed entity id (usr_…, app_…) — quiet copyable mono detail. */
  id: string;
  href?: string;
  className?: string;
  /**
   * Controls when the raw id is visually shown.
   * - "always" (default): id rendered as small mono text below the name.
   * - "hover": id hidden visually; accessible via tooltip (title attr) and
   *   sr-only text only. Copy affordance still works on click.
   *
   * Use "hover" for owner columns where the human name is sufficient and
   * the raw id (usr_…) should never appear as visible text.
   */
  idVisibility?: "always" | "hover";
}

/** Name-first entity reference. The raw id is never primary content: it
 * renders as a small mono click-to-copy affordance under the name. */
export function EntityRef({
  name,
  id,
  href,
  className,
  idVisibility = "always",
}: EntityRefProps) {
  const { copied, copy } = useCopied();

  if (idVisibility === "hover") {
    // Owner / human-name mode: the raw id must not appear as visible text.
    // Surface it only as a tooltip on the name and as an aria-label on a
    // visually-hidden (display:none) copy button so it never lands in
    // innerText or screen-reader announcement unless focus lands there.
    return (
      <span className={cn("inline-flex min-w-0 flex-col items-start", className)}>
        {href ? (
          <Link
            href={href}
            title={id}
            className="rounded-vf-sm text-vf-body text-vf-text hover:text-vf-accent focus-visible:ring-vf-accent/60 truncate focus-visible:ring-2 focus-visible:outline-none"
          >
            {name}
          </Link>
        ) : (
          <span className="text-vf-body text-vf-text truncate" title={id}>{name}</span>
        )}
        {/* Copy button: hidden from display but reachable via keyboard focus */}
        <button
          type="button"
          onClick={() => void copy(id)}
          title={copied ? "Copied" : id}
          aria-label={copied ? "Copied" : `Copy id: ${id}`}
          className="rounded-vf-sm text-vf-micro focus-visible:text-vf-subtle focus-visible:ring-vf-accent/60 hidden font-mono transition-colors focus-visible:inline-flex focus-visible:ring-2 focus-visible:outline-none"
        >
          <span aria-live="polite" className="sr-only">
            {copied ? "Copied to clipboard" : ""}
          </span>
        </button>
      </span>
    );
  }

  return (
    <span className={cn("inline-flex min-w-0 flex-col items-start", className)}>
      {href ? (
        <Link
          href={href}
          className="rounded-vf-sm text-vf-body text-vf-text hover:text-vf-accent focus-visible:ring-vf-accent/60 truncate focus-visible:ring-2 focus-visible:outline-none"
        >
          {name}
        </Link>
      ) : (
        <span className="text-vf-body text-vf-text truncate">{name}</span>
      )}
      <button
        type="button"
        onClick={() => void copy(id)}
        title={copied ? "Copied" : "Copy id"}
        className="rounded-vf-sm text-vf-micro text-vf-subtle hover:text-vf-muted focus-visible:ring-vf-accent/60 inline-flex max-w-full items-center gap-1 font-mono transition-colors focus-visible:ring-2 focus-visible:outline-none"
      >
        <span className="truncate">{id}</span>
        {copied && <CheckIcon className="text-vf-ok size-3 shrink-0" />}
        <span aria-live="polite" className="sr-only">
          {copied ? "Copied to clipboard" : ""}
        </span>
      </button>
    </span>
  );
}
