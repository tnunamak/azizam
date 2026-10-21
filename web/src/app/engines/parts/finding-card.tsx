"use client";

import { useId, useState, type ReactNode } from "react";
import { ArrowRight, CircleAlert, CircleX, Copy, RefreshCw } from "lucide-react";
import type { Finding, FindingAction } from "@/lib/engines-api";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { CopyButton, useCopied } from "@/components/ui/copy-button";

/** Raw server text and a request to repeat, folded under "Details". */
export function FindingDetails({
  raw,
  rawLabel,
  request,
  withKey,
  defaultOpen,
}: {
  raw: string;
  rawLabel: string;
  request?: string;
  withKey?: boolean;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(Boolean(defaultOpen));
  return (
    <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="mt-2">
      <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">Details</summary>
      <div className="mt-2 flex flex-col gap-3">
        <div>
          <p className="text-label text-muted-foreground mb-1">{rawLabel}</p>
          <pre className="bg-muted rounded-control text-meta max-h-56 overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]">
            {raw}
          </pre>
        </div>
        {request && (
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <p className="text-label text-muted-foreground">Repeat this request yourself</p>
              <CopyButton value={request} aria-label="Copy: Repeat this request yourself">
                Copy
              </CopyButton>
            </div>
            <pre className="bg-muted rounded-control text-meta overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]">
              {request}
            </pre>
          </div>
        )}
        {withKey && (
          <p className="text-meta text-muted-foreground">
            Replace YOUR_KEY with your key. Azizam never puts your key in this text.
          </p>
        )}
      </div>
    </details>
  );
}

function CopyNote({ text }: { text: string }) {
  const { copied, copy } = useCopied(text);
  return (
    <Button type="button" variant="outline" size="sm" aria-label="Copy a note for the owner" onClick={() => void copy(text)}>
      <Copy aria-hidden="true" />
      {copied ? "Copied" : "Copy a note for the owner"}
    </Button>
  );
}

/**
 * One finding, attached to the field at fault: what happened, why, what to do,
 * the server's own words under Details, and the one action that helps.
 */
export function FindingCard({
  finding,
  onFixPath,
  onOpenExisting,
  onRecheck,
  extraActions,
  textId,
  context,
}: {
  finding: Finding;
  onFixPath?: (action: FindingAction) => void;
  onOpenExisting?: (serviceId: string) => void;
  onRecheck?: () => void;
  extraActions?: ReactNode;
  /**
   * An id for the what, why and fix text (and the context line), so the field at fault
   * can list it in aria-describedby. The action buttons are not part of it.
   */
  textId?: string;
  /** One more line of help under the fix, in the same description. */
  context?: string;
}) {
  const titleId = useId();
  const warn = finding.severity === "warning";
  const Icon = warn ? CircleAlert : CircleX;
  const action = finding.action;
  return (
    <div
      data-testid="finding"
      role={warn ? "status" : "alert"}
      aria-labelledby={titleId}
      className={cn(
        "rounded-control min-w-0 border p-3",
        warn ? "border-warning/40 bg-warning-tint/50" : "border-danger/40 bg-danger-tint/50",
      )}
    >
      <p id={titleId} className="text-body flex items-center gap-2 font-medium">
        <Icon aria-hidden="true" className={cn("size-4 shrink-0", warn ? "text-warning" : "text-danger")} />
        <span>{finding.title}</span>
      </p>
      <div id={textId}>
        <dl className="text-meta mt-2 grid gap-x-3 gap-y-1 sm:grid-cols-[auto_1fr]">
          <dt className="text-muted-foreground">What happened</dt>
          <dd>{finding.what}</dd>
          <dt className="text-muted-foreground">Why</dt>
          <dd>{finding.why}</dd>
          <dt className="text-muted-foreground">What to do</dt>
          <dd>{finding.fix}</dd>
        </dl>
        {context && <p className="text-meta mt-2">{context}</p>}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 empty:hidden">
        {action?.id === "fixpath" && onFixPath && (
          <Button type="button" variant="outline" size="sm" onClick={() => onFixPath(action)}>
            {action.label}
          </Button>
        )}
        {action?.id === "openexisting" && action.service_id && onOpenExisting && (
          <Button type="button" variant="outline" size="sm" onClick={() => onOpenExisting(action.service_id as string)}>
            <ArrowRight aria-hidden="true" />
            {action.label}
          </Button>
        )}
        {warn && onRecheck && (
          <Button type="button" variant="outline" size="sm" onClick={onRecheck}>
            <RefreshCw aria-hidden="true" />
            Check again
          </Button>
        )}
        {finding.owner_note && <CopyNote text={finding.owner_note} />}
        {extraActions}
      </div>
      {finding.details && (
        <FindingDetails
          raw={finding.details.raw}
          rawLabel={finding.details.raw_label}
          request={finding.details.request}
          withKey={finding.details.with_key}
          defaultOpen={!warn}
        />
      )}
    </div>
  );
}
