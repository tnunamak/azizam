export const PLAN_LOCALITY = "Subscription plan";
export const PLAN_COST_LABEL = "Subscription plan · no API charge";

/**
 * Where a catalog model runs, as the Try page names it. A model a connected ChatGPT or Claude plan serves
 * (catalog cost kind "included") reads PLAN_LOCALITY, which costFrom turns into the no-API-charge label.
 */
export function localityLabel(record: Record<string, unknown>): string {
  const cost = record.cost;
  if (typeof cost === "object" && cost !== null && (cost as { kind?: unknown }).kind === "included")
    return PLAN_LOCALITY;
  if (record.locality === "local") return "Local";
  if (record.locality === "remote") {
    const provider = typeof record.provider === "string" && record.provider.trim() ? record.provider : "";
    return provider ? `Remote · ${provider}` : "Remote";
  }
  return "";
}

function formatCost(amount: unknown): string | undefined {
  if (typeof amount !== "number" || !Number.isFinite(amount) || amount < 0) return undefined;
  if (amount > 0 && amount < 0.000001) return "<$0.000001";
  return `$${amount.toFixed(amount < 0.01 && amount > 0 ? 6 : 2)}`;
}

export function costFrom(
  payload: unknown,
  response?: Response,
  locality?: string,
) {
  const object = typeof payload === "object" && payload !== null
    ? payload as Record<string, unknown> : {};
  if (locality === PLAN_LOCALITY) {
    const planAmount = (value: unknown): number | null | undefined => {
      if (value == null) return null;
      const raw = typeof value === "object"
        ? (value as { amount_usd?: unknown }).amount_usd : value;
      if (typeof raw === "number") return Number.isFinite(raw) && raw >= 0 ? raw : undefined;
      if (typeof raw !== "string") return undefined;
      const text = raw.trim();
      // Ignore radix notation rather than treating it as a measured dollar amount.
      if (/^0(?:x[\da-f]+|b[01]+|o[0-7]+)$/i.test(text)) return null;
      const decimal = /^[+-]?(\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.exec(text);
      if (!decimal) return undefined;
      const amount = Number(text);
      if (amount === 0 && /[1-9]/.test(decimal[1])) return undefined;
      return Number.isFinite(amount) && amount >= 0 ? amount : undefined;
    };
    const reported = response?.headers.get("x-litellm-response-cost");
    const header = planAmount(reported?.trim() || null);
    if (typeof header === "number" && header > 0) return formatCost(header);
    const amounts = [object.cost, object.remote_cost, object.measured_cost].map(planAmount);
    const positive = amounts.find((amount) => typeof amount === "number" && amount > 0);
    if (positive !== undefined) return formatCost(positive);
    if (header === 0 || amounts.includes(0) || (header === null && amounts.every((amount) => amount === null)))
      return PLAN_COST_LABEL;
    return undefined;
  }
  const cost = object.cost ?? object.remote_cost ?? object.measured_cost;
  const payloadAmount = typeof cost === "object" && cost !== null
    ? (cost as { amount_usd?: unknown }).amount_usd : cost;
  const positivePayload = Number.isFinite(Number(payloadAmount)) && Number(payloadAmount) > 0;
  const reported = response?.headers.get("x-litellm-response-cost");
  if (reported !== null && reported !== undefined && reported.trim()) {
    const amount = Number(reported);
    const maskedCharge = amount === 0 && locality === "Local" && positivePayload;
    if (Number.isFinite(amount) && amount >= 0 && !maskedCharge) {
      if (amount === 0 && locality === "Local")
        return "Local · no charge";
      return formatCost(amount);
    }
  }
  if (locality === "Local" && !positivePayload) return "Local · no charge";
  if (typeof payload !== "object" || payload === null) return undefined;
  if (typeof cost === "object" && cost !== null) {
    const amount = (cost as { amount_usd?: unknown }).amount_usd;
    if (typeof amount === "number")
      return formatCost(amount);
  }
  if (typeof cost === "number") return formatCost(cost);
  if (typeof cost === "string" && cost.trim()) return cost;
  return undefined;
}
