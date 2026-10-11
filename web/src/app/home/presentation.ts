import type {
  HomeAlias,
  HomeCapability,
  SetupProgress,
} from "./home-api";

const operations: Record<string, string[]> = {
  chat: ["chat", "chat.generate", "chat.completions"],
  image: ["images", "image", "image.generate"],
  video: ["videos", "video", "video.generate"],
  voice: [
    "speech",
    "tts",
    "speech.synthesize",
    "speech.synthesis",
    "audio.speech",
  ],
  transcribe: [
    "transcriptions",
    "transcribe",
    "stt",
    "speech.transcription",
    "speech.transcribe",
    "audio.transcribe",
  ],
  decide: [
    "decisions",
    "decide",
    "decision",
    "decision.decide",
    "decision.systemone",
    "systemone.decide",
  ],
  embed: ["embeddings", "embed", "text.embeddings"],
  rerank: ["rerank", "text.rerank"],
  audio: ["audio_generate", "audio", "audio.generate"],
};

/** A capability tile describes a generation alias, never an incidental workflow. */
export function capabilityTiles(
  capabilities: HomeCapability[],
  aliases: HomeAlias[],
): HomeCapability[] {
  // Current gateways supply one recommended row per capability.
  if (capabilities.some((row) => row.icon_key)) return capabilities;
  if (capabilities.some((row) => row.title)) {
    const order = [
      "chat",
      "image",
      "video",
      "music",
      "sfx",
      "speech",
      "voice",
      "audio",
      "transcribe",
      "decide",
      "embed",
      "rerank",
    ];
    const kinds = [
      ...new Set(capabilities.map((row) => row.capability)),
    ].toSorted((a, b) => order.indexOf(a) - order.indexOf(b));
    return kinds.flatMap((kind) => {
      const rows = capabilities.filter((row) => row.capability === kind);
      // Home rows are advertised in recommendation order; names are opaque.
      const row = rows[0];
      return row ? [{ ...row, model: row.use_name ?? row.model }] : [];
    });
  }
  return Object.entries(operations).flatMap<HomeCapability>(
    ([capability, intents]) => {
      const alias = aliases.find((item) => intents.includes(item.operation));
      if (alias)
        return [
          {
            capability,
            intent: alias.operation,
            model: alias.id,
            available: alias.available,
            status: alias.status,
            reason_code: alias.reason_code,
            availability: capabilities.find(
              (row) => row.model === alias.id && row.capability === capability,
            )?.availability,
          },
        ];
      const rows = capabilities.filter(
        (row) => row.capability === capability && intents.includes(row.intent),
      );
      const row =
        rows.find((item) => item.status === "ready") ??
        rows.find((item) => item.status === "starting") ??
        rows.find((item) => item.status === "busy") ??
        rows.find((item) => item.status === "degraded") ??
        rows[0];
      return row ? [{ ...row, model: null, workflow: null }] : [];
    },
  );
}

function joinNames(names: string[]): string {
  if (names.length < 3) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

export function capabilitySummary(tiles: HomeCapability[]): string {
  if (!tiles.length) return "No tasks are available to your account yet.";
  const names: Record<string, string> = {
    chat: "chat",
    image: "images",
    video: "video",
    voice: "voice",
    speech: "speech",
    music: "music",
    sfx: "sound effects",
    transcribe: "transcription",
    decide: "decisions",
    embed: "embeddings",
    rerank: "reranking",
    audio: "audio",
  };
  const clauses = (
    ["ready", "idle", "starting", "busy", "degraded", "unavailable"] as const
  ).flatMap((status) => {
    const matching = tiles
      .filter((tile) => tile.status === status)
      .map((tile) => names[tile.capability] ?? tile.capability);
    if (!matching.length) return [];
    const plural =
      matching.length > 1 ||
      ["images", "decisions", "embeddings"].includes(matching[0]);
    const description = status === "idle" ? "ready to start" : status;
    return [`${joinNames(matching)} ${plural ? "are" : "is"} ${description}`];
  });
  const summary = clauses.join(" · ");
  return summary.charAt(0).toUpperCase() + summary.slice(1);
}

/** Server observations and explicit confirmations remain distinct milestones. */
export function setupSteps(progress: SetupProgress) {
  const steps = progress.steps.filter((step) => step.id !== "claim_owner");
  return steps;
}

export function needsSetup(progress: SetupProgress): boolean {
  return (
    !progress.dismissed &&
    (!progress.complete ||
      setupSteps(progress).some((step) => !step.optional && !step.done))
  );
}

/** Which limit a refusal names: the gateway's warning says "daily", "monthly" or both. */
export function capLimitFrom(warning: string): "monthly" | "daily" | "unknown" {
  if (/monthly/i.test(warning)) return "monthly";
  if (/daily/i.test(warning)) return "daily";
  return "unknown";
}
