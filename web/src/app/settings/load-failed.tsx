"use client";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { usePageOutage } from "@/lib/page-outage";
import { readFailureCopy } from "@/lib/error-copy";

/**
 * A settings section whose read failed: one plain line, no card of its own.
 * When two or more sections failed together the page shows one error and one
 * "Try again" (see SettingsOutage), so the sections only say what did not load.
 */
export function SectionLoadFailed({
  thing,
  error,
  onRetry,
  retrying = false,
  className = "p-4",
}: {
  /** What did not load, in the person's words: "instance settings". */
  thing: string;
  error: unknown;
  onRetry: () => void;
  retrying?: boolean;
  className?: string;
}) {
  const { outage } = usePageOutage("settings");
  if (error instanceof ApiError && error.status === 403)
    return (
      <p role="alert" className={`text-muted-foreground text-meta ${className}`}>
        Only the owner can change this.
      </p>
    );
  return (
    <div className={`flex flex-wrap items-center justify-between gap-3 ${className}`}>
      <p role={outage ? undefined : "alert"} className="text-meta">
        <span className="font-medium">{readFailureCopy(thing).heading}.</span>{" "}
        <span className="text-muted-foreground">Nothing was changed.</span>
      </p>
      {outage ? null : (
        <Button
          variant="outline"
          size="sm"
          className="max-sm:min-h-11"
          disabled={retrying}
          onClick={onRetry}
          aria-label={`Try again to load ${thing}`}
        >
          Try again
        </Button>
      )}
    </div>
  );
}
