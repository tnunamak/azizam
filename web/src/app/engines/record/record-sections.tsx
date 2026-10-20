"use client";

/** The stored record, Remove and the save error text of the engine page. */
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { ApiError } from "@/lib/api";
import type { EngineView, SttChunking } from "@/lib/engines-api";

const CHUNKING_ROWS: { key: string; label: string; unit?: string }[] = [
  { key: "cut_files_longer_than_seconds", label: "Cuts recordings longer than", unit: "s" },
  { key: "target_seconds", label: "Distance between cuts", unit: "s" },
  { key: "search_seconds", label: "Looks for the quietest moment within", unit: "s" },
  { key: "max_segment_seconds", label: "Longest piece sent to the engine", unit: "s" },
];
const SAVED_KEY: Record<string, string> = { cut_files_longer_than_seconds: "threshold_seconds" };

/** How long recordings are cut for a transcription engine. Read-only; the values are saved in the engine's `chunking` settings. */
export function LongAudioSection({ chunking }: { chunking: SttChunking | null | undefined }) {
  if (!chunking) return null;
  return (
    <section aria-labelledby="engine-long-audio" className="flex flex-col gap-2">
      <h2 id="engine-long-audio" className="text-heading scroll-mt-20">Long recordings</h2>
      <div className="bg-card rounded-panel flex flex-col gap-2 border p-4">
        {"invalid" in chunking ? (
          <p className="text-meta text-muted-foreground">These settings are not valid: {chunking.invalid}</p>
        ) : (
          <>
            <p className="text-meta text-muted-foreground">
              {chunking.enabled
                ? "A recording longer than the engine takes is cut at the quietest moment near each target. Pieces do not overlap."
                : "Azizam does not cut recordings for this engine. It refuses audio over the hard maximum unless the engine is marked as taking long recordings whole."}
            </p>
            {chunking.enabled && (
              <dl className="text-body grid gap-x-3 gap-y-1 sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)]">
                {CHUNKING_ROWS.map(({ key, label, unit }) => (
                  <div key={key} className="contents">
                    <dt>{label}</dt>
                    <dd>
                      {String((chunking as unknown as Record<string, number>)[key])} {unit}
                      {chunking.from_bundled_defaults.includes(SAVED_KEY[key] ?? key) && (
                        <span className="text-meta text-muted-foreground"> (default, not saved on this engine)</span>
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            <dl className="text-body grid gap-x-3 gap-y-1 sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)]">
              <div className="contents">
                <dt>Takes long offline recordings whole</dt>
                <dd>{chunking.native_long_audio_offline ? "Yes" : "No"}</dd>
              </div>
              <div className="contents">
                <dt>Takes long streaming recordings whole</dt>
                <dd>{chunking.native_long_audio_streaming ? "Yes" : "No"}</dd>
              </div>
            </dl>
            <p className="text-meta text-muted-foreground">{chunking.note}</p>
          </>
        )}
      </div>
    </section>
  );
}

/**
 * What the gateway saved, for troubleshooting. It holds the keys Azizam stores
 * under, so it stays folded away until someone asks for it.
 */
export function StoredRecordSection({ view }: { view: EngineView }) {
  const otherSettings = Object.entries(view.other_settings ?? {});
  const hiddenSettings = view.hidden_settings ?? 0;
  return (
    <details className="bg-card rounded-panel border">
      <summary className="flex min-h-12 cursor-pointer items-center px-4 py-3">
        <h2 id="engine-stored" className="text-heading scroll-mt-20">Stored record</h2>
      </summary>
      <section aria-labelledby="engine-stored" className="flex flex-col gap-2 border-t p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-body font-medium">As saved</h3>
          <CopyButton value={JSON.stringify(view.record, null, 2)} aria-label="Copy the stored record">
            Copy
          </CopyButton>
        </div>
        <p className="text-meta text-muted-foreground">Saved configuration for troubleshooting. Secrets are hidden.</p>
        <p className="text-meta text-muted-foreground">Public settings and configured secret entries. Unclassified settings are omitted. You cannot edit this view here.</p>
        <pre
          tabIndex={0}
          aria-label="Stored record, read only"
          className="bg-muted rounded-control text-meta max-h-96 overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]"
        >
          {JSON.stringify(view.record, null, 2)}
        </pre>
        {view.v2 && (
          <div className="flex flex-col gap-1">
            <h3 className="text-body font-medium">Organized engine settings</h3>
            <p className="text-meta text-muted-foreground">The same settings grouped by connection, models and workflows, process control, checks, memory management, and resources.</p>
            <pre
              tabIndex={0}
              aria-label="Engine record, read only"
              className="bg-muted rounded-control text-meta max-h-96 overflow-auto p-3 font-mono whitespace-pre-wrap [overflow-wrap:anywhere]"
            >
              {JSON.stringify(view.v2, null, 2)}
            </pre>
          </div>
        )}
        {otherSettings.length > 0 && (
          <div>
            <h3 className="text-meta mb-1 font-medium">Other settings</h3>
            <p className="text-meta text-muted-foreground mb-2">
              These saved settings have no editor on this page.
            </p>
            <dl className="text-meta grid sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1">
              {otherSettings.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono [overflow-wrap:anywhere]">{key}</dt>
                  <dd className="[overflow-wrap:anywhere]">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        {hiddenSettings > 0 && (
          <p className="text-meta text-muted-foreground">
            {hiddenSettings} {hiddenSettings === 1 ? "setting" : "settings"} hidden: not classified
          </p>
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
        <p className="text-meta text-muted-foreground">This engine stops providing models or workflows to apps. Other engines may still provide the same public names.</p>
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
