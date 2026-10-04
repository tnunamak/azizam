import type {
  HomeAlias,
  HomeCapability,
  HomeReceipt,
  HomeSnapshot,
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
const preferredAliases: Record<string, string[]> = {
  chat: ["fast", "chat", "smart"],
  image: ["image"],
  video: ["video"],
  voice: ["speech", "tts", "voice"],
  transcribe: ["transcribe", "stt"],
  decide: ["decide", "decision"],
  embed: ["embed", "embedding"],
  rerank: ["rerank"],
  audio: ["audio"],
};

export function greetingName(user: HomeSnapshot["user"]): string {
  const name = user.name?.trim();
  return name && !name.includes("@") && name !== user.email
    ? name.split(/\s+/)[0]
    : "there";
}

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
    return kinds.map((kind) => {
      const rows = capabilities.filter((row) => row.capability === kind);
      const rank = (row: HomeCapability) =>
        (row.available ? 0 : 100) +
        (aliases.some((alias) => alias.id === row.model) ? 0 : 10);
      const row = rows.toSorted((a, b) => rank(a) - rank(b))[0];
      return { ...row, model: row.use_name ?? row.model };
    });
  }
  return Object.entries(operations).flatMap<HomeCapability>(
    ([capability, intents]) => {
      const candidates = aliases.filter((alias) =>
        intents.includes(alias.operation),
      );
      const ordered = candidates.toSorted((a, b) => {
        const rank = (alias: HomeAlias) => {
          const preferred = preferredAliases[capability].indexOf(alias.id);
          return (alias.available ? 0 : 100) + (preferred < 0 ? 50 : preferred);
        };
        return rank(a) - rank(b) || a.id.localeCompare(b.id);
      });
      const alias = ordered[0];
      if (alias)
        return [
          {
            capability,
            intent: alias.operation,
            model: alias.id,
            available: alias.available,
            status: alias.status,
            reason: alias.reason,
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
    ["ready", "idle", "starting", "busy", "unavailable"] as const
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

/** Receipts keep opaque routing identifiers as detail, beside a human request type. */
export function requestLabel(
  receipt: HomeReceipt,
  aliases: HomeAlias[],
): string {
  const alias = aliases.find((item) => item.id === receipt.requested_model);
  const operation = receipt.intent || alias?.operation || "";
  const capability = Object.entries(operations).find(([, intents]) =>
    intents.includes(operation),
  )?.[0];
  const requestTypes: Array<[string, RegExp, RegExp]> = [
    ["image", /^image\./, /\/images\//],
    ["video", /^video\./, /\/videos/],
    ["transcribe", /transcri/, /\/audio\/transcriptions/],
    ["voice", /^speech\.|^audio\.speech/, /\/audio\/speech/],
    ["audio", /^audio\./, /\/audio\/generations/],
    ["decide", /^(decision|systemone)/, /\/(systemone|decisions)/],
    ["embed", /embedding/, /\/embeddings/],
    ["rerank", /rerank/, /\/rerank/],
    [
      "chat",
      /^(chat\.|responses\.|text\.completions)/,
      /\/(chat\/completions|responses|completions)/,
    ],
  ];
  const kind =
    capability ||
    requestTypes.find(
      ([, intent, path]) =>
        intent.test(operation) || path.test(receipt.path || ""),
    )?.[0];
  const labels: Record<string, string> = {
    chat: "Chat request",
    image: "Image request",
    video: "Video request",
    voice: "Voice request",
    transcribe: "Transcription request",
    decide: "Decision request",
    embed: "Embedding request",
    rerank: "Rerank request",
    audio: "Audio request",
  };
  return kind ? labels[kind] : "Request";
}

/** Server observations and explicit confirmations remain distinct milestones. */
export function setupSteps(progress: SetupProgress) {
  const steps = progress.steps.filter((step) => step.id !== "claim_owner");
  if (!progress.evidence) return steps;
  if (progress.evidence.observed_external_client?.recorded) return steps;
  const copied = {
    id: "copy_settings",
    done: progress.evidence.settings_copied.recorded,
    optional: false,
    reason: progress.evidence.settings_copied.recorded
      ? "You confirmed that the settings were copied."
      : "Copy the settings into your app, then confirm below.",
    href: "#connect",
  };
  const viewed = {
    id: "view_receipt",
    done: progress.evidence.receipt_viewed.recorded,
    optional: false,
    reason: progress.evidence.receipt_viewed.recorded
      ? "You confirmed that you viewed a request receipt."
      : "Open a request receipt, then confirm that you viewed it.",
    href: "#setup-heading",
  };
  const clientConfirmed = {
    id: "connect_app",
    done: progress.evidence.external_client_confirmed.recorded,
    optional: false,
    reason: progress.evidence.external_client_confirmed.recorded
      ? "You confirmed that your app sent a request."
      : "Send one request from your app, then confirm below.",
    href: "#connect",
  };
  return [
    ...steps.filter(
      (step) => !["connect_app", "invite_someone"].includes(step.id),
    ),
    copied,
    clientConfirmed,
    viewed,
    ...steps.filter((step) => step.id === "invite_someone"),
  ];
}

export function hasEstablishedTraffic(home: HomeSnapshot): boolean {
  return home.receipts.some((receipt) => Boolean(receipt.request_id));
}

export function needsSetup(progress: SetupProgress): boolean {
  return (
    !progress.dismissed &&
    (!progress.complete ||
      setupSteps(progress).some((step) => !step.optional && !step.done))
  );
}

export function shouldShowSetup(
  progress: SetupProgress,
  home: HomeSnapshot,
): boolean {
  return needsSetup(progress) && !hasEstablishedTraffic(home);
}
