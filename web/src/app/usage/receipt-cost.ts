/**
 * What a spend cell says, from the counts the gateway sends. `usd` is the sum of
 * reported costs, or null when none was reported. `unknownPaid` counts remote requests
 * with no reported cost, and `local` counts requests whose receipts show they were served
 * on this machine. A null count means the gateway did not say, so the cell claims nothing
 * about where those requests ran.
 */
export type CostState =
  | { kind: "amount"; usd: number; excluded: number; local: number }
  | { kind: "not-reported"; excluded: number; local: number }
  | { kind: "local"; local: number }
  | { kind: "unrecorded"; local: number };

export interface CostCounts {
  usd: number | null;
  unknownPaid: number | null;
  local: number | null;
  requests: number | null;
}

/**
 * One rule for a receipt and for a group of them. A group where every request was served
 * here and nothing was charged says Local · no charge. Otherwise it shows the reported
 * amount, or why there is none. A local request never makes a remote one look free.
 */
export function costState({ usd, unknownPaid, local, requests }: CostCounts): CostState {
  const excluded = unknownPaid ?? 0;
  const localCount = local ?? 0;
  if (usd !== null && usd > 0) return { kind: "amount", usd, excluded, local: localCount };
  if (requests !== null && requests > 0 && localCount === requests && excluded === 0)
    return { kind: "local", local: localCount };
  if (usd === 0 && excluded > 0) return { kind: "not-reported", excluded, local: localCount };
  if (usd !== null) return { kind: "amount", usd, excluded, local: localCount };
  if (excluded > 0) return { kind: "not-reported", excluded, local: localCount };
  return { kind: "unrecorded", local: localCount };
}

export const costCopy: Record<Exclude<CostState["kind"], "amount">, { label: string; help: string }> = {
  local: {
    label: "Local · no charge",
    help: "Didn’t go to a paid provider, so nothing was charged.",
  },
  "not-reported": {
    label: "Cost not reported by the provider",
    help: "A paid provider handled this request but didn’t report what it cost, so it isn’t in the total.",
  },
  unrecorded: {
    label: "Cost not recorded",
    help: "Azizam didn’t record where this request ran, so it can’t say whether it cost money.",
  },
};


/** Normalized receipt/CSV amount: preserve its historical top-field-only rule. */
export function receiptCostUsd(raw: unknown): number | null {
  const data = object(raw);
  const metadata = object(data.metadata);
  return nullableNumber(
    data.remote_spend ??
      data.cost_usd ??
      data.remote_cost_usd ??
      metadata.cost_usd,
  );
}

/** A raw receipt's display cost, including the legacy nested-cost fallback. */
export function receiptCost(raw: unknown): CostState {
  const data = object(raw);
  const cost = object(data.cost);
  const reported = receiptCostUsd(raw) ?? nullableNumber(cost.usd);
  const unreported = reported === null && (data.remote === true || cost.cost_class === "paid-remote");
  return costState({
    usd: reported,
    unknownPaid: unreported ? 1 : 0,
    local: data.served_locally === true ? 1 : 0,
    requests: 1,
  });
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}


function nullableNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}
