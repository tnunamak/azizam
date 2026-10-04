import type { CurrentHealthService, Service } from "@/lib/api";

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  completions: "Text",
  image: "Images",
  images: "Images",
  video: "Video",
  speech: "Speech",
  tts: "Speech",
  transcribe: "Transcribe",
  transcription: "Transcribe",
  stt: "Transcribe",
  embeddings: "Embeddings",
  embed: "Embeddings",
  rerank: "Rerank",
  decide: "Decisions",
  decisions: "Decisions",
};

export function humanCapability(capability: string): string {
  return (
    CAPABILITY_LABELS[capability.toLowerCase()] ??
    capability
      .replace(/[._-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
}

export type EngineEntry = { service: Service; alternatives: Service[] };
export type EngineGroup = { capability: string; entries: EngineEntry[] };

/** Registry roles and priority determine grouping; engine names never do. */
export function groupEngines(
  services: Service[],
  health: Map<string, CurrentHealthService>,
  showNotInUse: boolean,
  selectedId: string | null,
): { groups: EngineGroup[]; hiddenCount: number } {
  const notInUse = (service: Service) =>
    !service.enabled || health.get(service.id)?.configured === false;
  const hiddenCount = services.filter(notInUse).length;
  const visible = services.filter(
    (service) =>
      showNotInUse || !notInUse(service) || service.id === selectedId,
  );
  const byCapability = new Map<string, Service[]>();
  for (const service of visible) {
    // A multi-capability engine appears once, under its first declared capability.
    const capability = service.capabilities.length
      ? humanCapability(service.capabilities[0])
      : "Other engines";
    const rows = byCapability.get(capability) ?? [];
    rows.push(service);
    byCapability.set(capability, rows);
  }
  const groups = Array.from(byCapability, ([capability, rows]) => {
    const sorted = [...rows].sort(
      (a, b) =>
        (a.priority ?? 100) - (b.priority ?? 100) ||
        a.name.localeCompare(b.name),
    );
    const isAlternative = (service: Service) =>
      service.role === "standby" || service.role === "fallback";
    const entries = sorted
      .filter((service) => !isAlternative(service))
      .map((service) => ({ service, alternatives: [] as Service[] }));
    for (const service of sorted.filter(isAlternative)) {
      const explicitParent =
        service.config.primary_service_id ?? service.config.fallback_for;
      const primary =
        typeof explicitParent === "string"
          ? entries.find((entry) => entry.service.id === explicitParent)
          : entries.find(
              (entry) =>
                entry.service.enabled &&
                (entry.service.role === "primary" ||
                  entry.service.role === "required"),
            );
      if (primary) primary.alternatives.push(service);
      else entries.push({ service, alternatives: [] });
    }
    return { capability, entries };
  }).sort((a, b) => a.capability.localeCompare(b.capability));
  return { groups, hiddenCount };
}
