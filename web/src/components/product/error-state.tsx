"use client";
import { AlertCircle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { errorCopy } from "@/lib/error-copy";

export interface ErrorStateProps {
  message?: string;
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
}: ErrorStateProps) {
  const copy = errorCopy(error, { heading, role: viewerRole });
  const recovery = copy.recovery;
  const retry = (recovery === "retry" || recovery === "refresh") && onRetry;
  return (
    <div
      role="alert"
      data-slot="alert"
      className="bg-card flex min-w-0 items-start gap-3.5 rounded-panel border p-4 sm:p-5"
    >
      <span className="bg-danger-tint text-danger flex size-9 shrink-0 items-center justify-center rounded-full">
        <AlertCircle aria-hidden="true" className="size-4.5" />
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-semibold">
          {heading ?? (error ? copy.heading : "Couldn’t load this")}
        </p>
        <div className="text-muted-foreground space-y-3 text-sm">
          <p>
            {message ??
              (error
                ? copy.message
                : onRetry
                  ? "Check your connection."
                  : "Check your connection and reload this page.")}
          </p>
          {action ? (
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
              <a href="/ui/sign-in/">Sign in</a>
            </Button>
          ) : (
            retry && (
              <Button
                variant="outline"
                size="sm"
                disabled={retrying}
                onClick={onRetry}
              >
                {retrying && (
                  <Loader2 className="animate-spin" data-icon="inline-start" />
                )}{" "}
                {retrying
                  ? "Trying again…"
                  : recovery === "refresh"
                    ? "Refresh"
                    : "Try again"}
              </Button>
            )
          )}
          {copy.details && (
            <details>
              <summary className="min-h-11 cursor-pointer py-3">
                Technical details
              </summary>
              <p className="font-mono break-words">{copy.details}</p>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
