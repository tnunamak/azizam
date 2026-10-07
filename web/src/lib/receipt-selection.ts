function canonicalFields(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalFields);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, field]) => [key, canonicalFields(field)]),
    );
  }
  return value;
}

/**
 * Selection keys for receipt rows. A unique real ID becomes `id:<id>`; any
 * other row gets `row:<content>`, built from the receipt plus optional row
 * context (person, app, time), never from list position. The two prefixes keep
 * real IDs and generated keys apart, so no refresh can hand one row's key to a
 * different row. Identical rows get numbered suffixes in list order.
 */
export function receiptSelectionKeys(
  receipts: Record<string, unknown>[],
  contexts?: unknown[],
): string[] {
  const ids = receipts.map((receipt) => {
    const id = receipt.request_id ?? receipt.id;
    return typeof id === "string" && id.trim() ? id : null;
  });
  const counts = new Map<string, number>();
  for (const id of ids) {
    if (id !== null) counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const used = new Set<string>();
  return receipts.map((receipt, index) => {
    const id = ids[index];
    let key: string;
    if (id !== null && counts.get(id) === 1) {
      key = `id:${id}`;
    } else {
      const identity =
        contexts === undefined ? receipt : { receipt, context: contexts[index] };
      const base = `row:${JSON.stringify(canonicalFields(identity))}`;
      key = base;
      let suffix = 1;
      while (used.has(key)) key = `${base}:${suffix++}`;
    }
    used.add(key);
    return key;
  });
}
