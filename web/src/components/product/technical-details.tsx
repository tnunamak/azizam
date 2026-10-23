/** Raw answers and status codes, folded away from the normal view (P15). Try's results and History's requests share it. */
export function TechnicalDetails({ text }: { text: string }) {
  return (
    <details className="border-t pt-3">
      <summary className="min-h-11 cursor-pointer text-sm font-medium">
        Technical details
      </summary>
      <pre
        tabIndex={0}
        className="bg-muted max-h-80 overflow-auto rounded-lg p-3 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {text}
      </pre>
    </details>
  );
}
