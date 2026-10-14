export interface MoneyProps {
  amount: number | null | undefined;
  currency?: string;
  local?: boolean;
  /** Omit absent measurements in compact summaries; details retain the gap. */
  summary?: boolean;
  /** Preserve small Usage amounts without making tiny charges look free. */
  precise?: boolean;
}
export function Money({
  amount,
  currency = "USD",
  local = false,
  summary = false,
  precise = false,
}: MoneyProps) {
  if (local) return <span>Local · no provider charge</span>;
  if (amount == null || !Number.isFinite(amount))
    return summary ? null : (
      <span className="text-muted-foreground">Not measured yet</span>
    );
  if (precise && amount > 0 && amount < 0.0001)
    return <span className="tabular-nums">{`<${new Intl.NumberFormat("en-US", {
      style: "currency", currency, minimumFractionDigits: 4, maximumFractionDigits: 4,
    }).format(0.0001)}`}</span>;
  return (
    <span className="tabular-nums">
      {new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: precise && Math.abs(amount) < 1
          ? 4 : amount !== 0 && Math.abs(amount) < 0.01 ? 6 : 2,
      }).format(amount)}
    </span>
  );
}
