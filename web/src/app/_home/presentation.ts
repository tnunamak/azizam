import type {
  HomeCapability,
  SetupProgress,
} from "./home-api";

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
    mesh: "3D models",
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
