/**
 * The way to a run's receipt. The id and its copy action live in the
 * receipt's own details, not in the normal result (PF-010).
 */
export function ReceiptLink({ id }: { id: string }) {
  return (
    <a
      className="inline-flex min-h-11 items-center underline underline-offset-4"
      href={`/r/${encodeURIComponent(id)}`}
    >
      View receipt
    </a>
  );
}
