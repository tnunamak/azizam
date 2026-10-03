export interface MoneyProps {
  amount: number | null | undefined;
  currency?: string;
  local?: boolean;
}
export function Money({ amount, currency = "USD", local = false }: MoneyProps) {
  if (local) return <span>Free (your hardware)</span>;
  if (amount == null || !Number.isFinite(amount))
    return <span className="text-muted-foreground">Not measured yet</span>;
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
