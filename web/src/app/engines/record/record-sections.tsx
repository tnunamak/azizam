"use client";

/** Pieces of the engine page that the editable and the read-only page share. */
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { ApiError } from "@/lib/api";
import type { EngineView } from "@/lib/engines-api";

/**
 * What the gateway saved, for troubleshooting. It holds the keys Azizam stores
 * under, so it stays folded away until someone asks for it.
 */
export function StoredRecordSection({ view }: { view: EngineView }) {
  const otherSettings = Object.entries(view.other_settings ?? {});
  return (
    <details className="bg-card rounded-panel border">
      <summary className="text-heading flex min-h-12 cursor-pointer items-center px-4 py-3">Technical details</summary>
      <section aria-labelledby="engine-stored" className="flex flex-col gap-2 border-t p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="engine-stored" className="text-body font-medium">Stored record</h2>
          <CopyButton value={JSON.stringify(view.record, null, 2)} aria-label="Copy the stored record">
            Copy
          </CopyButton>
        </div>
        <p className="text-meta text-muted-foreground">Saved configuration for troubleshooting. Secrets are hidden.</p>
        <p className="text-meta text-muted-foreground">Exactly what the gateway saved. You cannot edit it here.</p>
        <pre
          tabIndex={0}
          aria-label="Stored record, read only"
          className="bg-muted rounded-control text-meta max-h-96 overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]"
        >
          {JSON.stringify(view.record, null, 2)}
        </pre>
        {otherSettings.length > 0 && (
          <div>
            <h3 className="text-meta mb-1 font-medium">Other settings</h3>
            <p className="text-meta text-muted-foreground mb-2">
              Azizam kept these values, but this form has no field for them. They are not lost.
            </p>
            <dl className="text-meta grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1">
              {otherSettings.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono [overflow-wrap:anywhere]">{key}</dt>
                  <dd className="[overflow-wrap:anywhere]">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </section>
    </details>
  );
}

export function RemoveSection({ onRemove }: { onRemove: () => void }) {
  return (
    <section aria-labelledby="engine-remove" className="bg-card rounded-panel flex flex-wrap items-center justify-between gap-3 border p-4">
      <div>
        <h2 id="engine-remove" className="text-heading">Remove this engine</h2>
        <p className="text-meta text-muted-foreground">Apps lose its models at once. You can add it again later.</p>
      </div>
      <Button type="button" variant="destructive" size="sm" onClick={onRemove}>
        Remove engine
      </Button>
    </section>
  );
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) return "Engine settings changed elsewhere. Refresh and try again.";
    return error.message;
  }
  return error instanceof Error ? error.message : "The gateway did not answer. Try again.";
}
