"use client";
import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
    <Alert variant="destructive">
      <AlertCircle aria-hidden="true" />
      <AlertTitle>
        {heading ?? (error ? copy.heading : "Couldn’t load this")}
      </AlertTitle>
      <AlertDescription>
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
            <Button variant="outline" asChild>
              <a href={action.href}>{action.label}</a>
            </Button>
          ) : (
            <Button
              variant="outline"
              onClick={action.onClick}
              disabled={retrying}
            >
              {action.label}
            </Button>
          )
        ) : recovery === "sign-in" ? (
          <Button variant="outline" asChild>
            <a href="/ui/sign-in/">Sign in</a>
          </Button>
        ) : (
          retry && (
            <Button variant="outline" disabled={retrying} onClick={onRetry}>
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
          <details className="text-sm">
            <summary className="min-h-11 cursor-pointer py-3">
              Technical details
            </summary>
            <p className="font-mono break-words">{copy.details}</p>
          </details>
        )}
      </AlertDescription>
    </Alert>
  );
}
