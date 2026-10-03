"use client";
import { AlertCircle, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export interface ErrorStateProps {
  message?: string;
  onRetry?: () => void;
  retrying?: boolean;
}
export function ErrorState({
  message = "We couldn't load this page. Try again.",
  onRetry,
  retrying = false,
}: ErrorStateProps) {
  return (
    <Alert variant="destructive">
      <AlertCircle aria-hidden="true" />
      <AlertTitle>Couldn’t load this</AlertTitle>
      <AlertDescription>
        <p>{message}</p>
        {onRetry && (
          <Button variant="outline" disabled={retrying} onClick={onRetry}>
            {retrying && (
              <Loader2 className="animate-spin" data-icon="inline-start" />
            )}{" "}
            {retrying ? "Trying again…" : "Try again"}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
