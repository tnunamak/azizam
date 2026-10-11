import type { CurrentHealthService, Service } from "@/lib/api";

const CAPABILITY_LABELS: Record<string, string> = {
  chat: "Chat",
  completions: "Text",
  text: "Text",
  audio: "Audio",
  transcriptions: "Transcription",
  generations: "Images",
  videos: "Video",
  realtime: "Realtime",
  image: "Images",
  images: "Images",
  video: "Video",
  speech: "Voice",
  tts: "Voice",
  transcribe: "Transcription",
  transcription: "Transcription",
  stt: "Transcription",
  embeddings: "Embeddings",
  embed: "Embeddings",
  rerank: "Reranking",
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

export function humanCapabilities(capabilities: string[]): string[] {
  const labels: string[] = [];
  for (const capability of capabilities) {
    // Summaries use the stable public capability vocabulary. Custom registry
    // tags remain available in Details and Advanced fields.
    const label = CAPABILITY_LABELS[capability.toLowerCase()];
    if (!label) continue;
    if (!labels.includes(label)) labels.push(label);
  }
  return labels;
}

function primaryHumanCapability(capabilities: string[]): string | null {
  return humanCapabilities(capabilities)[0] ?? null;
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
    const capability =
      primaryHumanCapability(service.capabilities) ?? "Other engines";
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
