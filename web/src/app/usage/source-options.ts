/**
 * The Source picker's list. Sources doing work come first, busiest first;
 * turned-off apps and apps with no requests this month come last, under their
 * own heading. Apps that share a name are told apart by a number, in the order
 * they were created.
 *
 * A connection (an app someone approved by signing in, such as ChatGPT) is a
 * source of its own and is listed apart from apps, as Apps & keys lists it.
 * One that was disconnected stays while it has requests to show.
 */

export interface UsageSource {
  id: string;
  label: string;
  /** "app" or "connection"; the server names no other kind in this list. */
  kind: string;
  /** Requests this month in the current person scope; null when not reported. */
  requests: number | null;
  disabled: boolean;
  createdAt: number | null;
}

export interface SourceOption {
  id: string;
  label: string;
}

export interface SourceGroups {
  active: SourceOption[];
  connections: SourceOption[];
  inactive: SourceOption[];
}

function byLabel(a: UsageSource, b: UsageSource): number {
  return a.label.localeCompare(b.label) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

function busiestFirst(a: UsageSource, b: UsageSource): number {
  return (b.requests ?? 0) - (a.requests ?? 0) || byLabel(a, b);
}

/** "Daisy", "Daisy (2)": the number follows creation order, so it is stable. */
function uniqueLabels(sources: UsageSource[]): Map<string, string> {
  const byName = new Map<string, UsageSource[]>();
  for (const source of sources) {
    byName.set(source.label, [...(byName.get(source.label) ?? []), source]);
  }
  const labels = new Map<string, string>();
  const reserved = new Set(sources.map((source) => source.label));
  for (const [label, same] of byName) {
    const created = [...same].sort(
      (a, b) =>
        (a.createdAt ?? 0) - (b.createdAt ?? 0) || (a.id < b.id ? -1 : 1),
    );
    let suffix = 1;
    for (const source of created) {
      let unique = label;
      if (same.length > 1) {
        do { unique = `${label} (${suffix++})`; } while (reserved.has(unique));
        reserved.add(unique);
      }
      labels.set(source.id, unique);
    }
  }
  return labels;
}

export function sourceGroups(all: UsageSource[]): SourceGroups {
  const sources = all.filter((s) => s.kind !== "connection");
  const connections = all.filter((s) => s.kind === "connection");
  const labels = uniqueLabels(sources);
  const connectionLabels = uniqueLabels(connections);
  const option = (source: UsageSource): SourceOption => ({
    id: source.id,
    label: labels.get(source.id) ?? source.label,
  });
  const isInactive = (source: UsageSource) =>
    source.disabled || source.requests === 0;
  return {
    active: sources.filter((s) => !isInactive(s)).sort(busiestFirst).map(option),
    connections: [...connections].sort(busiestFirst).map((source) => ({
      id: source.id,
      label: `${connectionLabels.get(source.id) ?? source.label}${source.disabled ? " (disconnected)" : ""}`,
    })),
    inactive: sources.filter(isInactive).sort(busiestFirst).map(option),
  };
}
