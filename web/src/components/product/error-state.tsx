"use client";
import { Loader2 } from "lucide-react";
import { Callout } from "./status-ui";
import { Button } from "@/components/ui/button";
import { errorCopy } from "@/lib/error-copy";

export interface ErrorStateProps {
  /** null hides the sentence when the heading and button say enough. */
  message?: string | null;
  /** critical for failures; info for expected states such as signing in. */
  severity?: "critical" | "attention" | "info";
  heading?: string;
  error?: unknown;
  viewerRole?: "member" | "owner" | "admin";
  action?: { label: string } & (
    | { onClick: () => void; href?: never }
    | { href: string; onClick?: never }
  );
  onRetry?: () => void;
  retrying?: boolean;
}
export function ErrorState({
  message,
  heading,
  error,
  viewerRole = "member",
  action,
  onRetry,
  retrying = false,
  severity = "critical",
}: ErrorStateProps) {
  const copy = errorCopy(error, { heading, role: viewerRole });
  const recovery = copy.recovery;
  const retry = (recovery === "retry" || recovery === "refresh") && onRetry;
  const actionNode = action ? (
    action.href ? (
      <Button variant="outline" size="sm" asChild>
        <a href={action.href}>{action.label}</a>
      </Button>
    ) : (
      <Button
        variant="outline"
        size="sm"
        onClick={action.onClick}
        disabled={retrying}
      >
        {action.label}
      </Button>
    )
  ) : recovery === "sign-in" ? (
    <Button variant="outline" size="sm" asChild>
      <a href="/sign-in/">Sign in</a>
    </Button>
  ) : retry ? (
    <Button variant="outline" size="sm" disabled={retrying} onClick={onRetry}>
      {retrying && <Loader2 className="animate-spin" data-icon="inline-start" />}{" "}
      {retrying
        ? "Trying again…"
        : recovery === "refresh"
          ? "Refresh"
          : "Try again"}
    </Button>
  ) : null;
  return (
    <Callout
      as="div"
      severity={severity}
      title={heading ?? (error ? copy.heading : "Something didn’t load")}
      actions={actionNode}
    >
      {message === null ? null : (
        <p className="text-muted-foreground">
          {message ??
            (error
              ? copy.message
              : `We couldn't tell where this stopped. ${onRetry ? "Try again." : "Reload this page."}`)}
        </p>
      )}
      {copy.details && (
        <details className="text-muted-foreground">
          <summary className="flex min-h-8 w-fit cursor-pointer items-center max-sm:min-h-11">
            Technical details
          </summary>
          <p className="font-mono break-words">{copy.details}</p>
        </details>
      )}
    </Callout>
  );
}
