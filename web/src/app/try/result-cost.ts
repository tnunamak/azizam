export function costFrom(
  payload: unknown,
  response?: Response,
  locality?: string,
) {
  const object = typeof payload === "object" && payload !== null
    ? payload as Record<string, unknown> : {};
  const cost = object.cost ?? object.remote_cost ?? object.measured_cost;
  const payloadAmount = typeof cost === "object" && cost !== null
    ? (cost as { amount_usd?: unknown }).amount_usd : cost;
  const positivePayload = Number.isFinite(Number(payloadAmount)) && Number(payloadAmount) > 0;
  const reported = response?.headers.get("x-litellm-response-cost");
  if (reported !== null && reported !== undefined && reported.trim()) {
    const amount = Number(reported);
    if (Number.isFinite(amount) && amount >= 0 && !(amount === 0 && locality === "Local" && positivePayload)) {
      if (amount === 0 && locality === "Local")
        return "Local · no provider charge";
      return `$${amount.toFixed(amount < 0.01 && amount > 0 ? 6 : 2)}`;
    }
  }
  if (locality === "Local" && !positivePayload) return "Local · no provider charge";
  if (typeof payload !== "object" || payload === null) return undefined;
  if (typeof cost === "object" && cost !== null) {
    const amount = (cost as { amount_usd?: unknown }).amount_usd;
    if (typeof amount === "number")
      return amount === 0 ? "$0.00" : `$${amount}`;
  }
  if (typeof cost === "number") return cost === 0 ? "$0.00" : `$${cost}`;
  if (typeof cost === "string" && cost.trim()) return cost;
  return undefined;
}
