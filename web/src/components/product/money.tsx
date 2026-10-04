export interface MoneyProps {
  amount: number | null | undefined;
  currency?: string;
  local?: boolean;
  /** Omit absent measurements in compact summaries; details retain the gap. */
  summary?: boolean;
}
export function Money({
  amount,
  currency = "USD",
  local = false,
  summary = false,
}: MoneyProps) {
  if (local) return <span>Local · no provider charge</span>;
  if (amount == null || !Number.isFinite(amount))
    return summary ? null : (
      <span className="text-muted-foreground">Not measured yet</span>
    );
  return (
    <span className="tabular-nums">
      {new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: amount !== 0 && Math.abs(amount) < 0.01 ? 6 : 2,
      }).format(amount)}
    </span>
  );
}
