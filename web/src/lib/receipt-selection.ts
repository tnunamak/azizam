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

/** Keep unique real IDs; reserve all IDs before assigning fallback keys. */
export function receiptSelectionKeys(
  receipts: Record<string, unknown>[],
): string[] {
  const ids = receipts.map((receipt) => {
    const id = receipt.request_id ?? receipt.id;
    return typeof id === "string" && id.trim() ? id : null;
  });
  const reserved = new Set(ids.filter((id) => id !== null));
  const counts = new Map<string, number>();
  for (const id of ids) {
    if (id !== null) counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const used = new Set<string>();
  return receipts.map((receipt, index) => {
    const id = ids[index];
    let key = id && counts.get(id) === 1 ? id : null;
    if (key === null) {
      const base = `receipt:${index}:${JSON.stringify(canonicalFields(receipt))}`;
      key = base;
      let suffix = 1;
      while (reserved.has(key) || used.has(key)) key = `${base}:${suffix++}`;
    }
    used.add(key);
    return key;
  });
}
