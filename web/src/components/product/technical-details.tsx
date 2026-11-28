import { useViewer } from "@/components/shell/session";
import { hideHostPaths } from "@/lib/host-path-text";

/**
 * Raw answers and status codes, folded away from the normal view (P15). Try's results and History's requests share it.
 * Only the owner reads host paths here: an engine can name the file it loaded or a folder in an error, so every other
 * viewer reads each path cut to its file name.
 */
export function TechnicalDetails({ text }: { text: string }) {
  const owner = useViewer().role === "owner";
  return (
    <details className="border-t pt-3">
      <summary className="min-h-11 cursor-pointer text-sm font-medium">
        Technical details
      </summary>
      <pre
        tabIndex={0}
        className="bg-muted max-h-80 overflow-auto rounded-lg p-3 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {owner ? text : hideHostPaths(text)}
      </pre>
    </details>
  );
}
