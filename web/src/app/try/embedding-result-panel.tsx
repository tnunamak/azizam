"use client";

import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { summarizeEmbedding } from "@/app/try/embedding-result";

/** The body of an embedding result; the result card above it carries the heading. */
export function EmbeddingResultPanel({ payload }: { payload: unknown }) {
  const summary = summarizeEmbedding(payload);
  if (summary === null) return <p>No numbers came back.</p>;

  const download = () => {
    const blob = new Blob([summary.json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    try {
      anchor.href = url;
      anchor.download = "embedding.json";
      document.body.appendChild(anchor);
      anchor.click();
    } finally {
      anchor.remove();
      // Let the browser start reading the download before releasing its URL.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  };

  return (
    <section className="grid gap-3">
      <p>Apps compare these numbers to find similar text or to search documents.</p>
      <dl className="grid gap-2">
        <div><dt>Model</dt><dd>{summary.model ?? "Not reported"}</dd></div>
        <div><dt>Inputs</dt><dd>{summary.inputs}</dd></div>
        <div><dt>Numbers per input</dt><dd>{summary.dimensions}</dd></div>
      </dl>
      <p className="break-words font-mono">
        {summary.preview.map(number => number.toFixed(4)).join(", ")}
        {summary.more > 0 ? ` and ${summary.more} more` : ""}
      </p>
      <div className="flex flex-wrap gap-2">
        <CopyButton value={summary.json}>Copy</CopyButton>
        <Button type="button" variant="outline" onClick={download}>Download</Button>
      </div>
    </section>
  );
}
