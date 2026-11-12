import { capabilityTaskNames } from "./capability-words";

/** Capability labels use the gateway capability IDs and VOICE.md vocabulary. */
export const capabilityLabels = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  mesh: "3D models",
  speech: "Voice",
  transcribe: "Transcription",
  music: "Music",
  sfx: "Sound effects",
  audio: "Audio",
  decide: "Decisions",
  embed: "Document search",
  rerank: "Reranking",
} as const;

type CapabilityId = keyof typeof capabilityLabels;

const capabilityAliases: Record<string, CapabilityId> = {
  images: "image",
  generations: "image",
  videos: "video",
  tts: "speech",
  transcription: "transcribe",
  transcriptions: "transcribe",
  stt: "transcribe",
  sound_effects: "sfx",
  decisions: "decide",
  embeddings: "embed",
  reranking: "rerank",
};

const capabilityLabelAliases: Record<string, string> = {
  voice: capabilityLabels.speech,
};

/** Resolve a gateway capability ID or a known protocol spelling to its label. */
export function capabilityLabelFor(key: string | null | undefined): string | undefined {
  const normalized = String(key ?? "").toLowerCase();
  const id = capabilityAliases[normalized] ?? normalized;
  return capabilityLabelAliases[normalized] ?? capabilityLabels[id as CapabilityId];
}

/**
 * What a person asks for, in the words Home and Try use. Screens that name a
 * kind of request (Usage rows, chart legends) use these, so one thing keeps
 * one name across the app.
 */
// The words live in registry/capability_words.yml (task_name, else member_label, else label); this keeps the client's keys.
export const taskNames = {
  chat: capabilityTaskNames.chat,
  image: capabilityTaskNames.image,
  video: capabilityTaskNames.video,
  mesh: capabilityTaskNames.mesh,
  speech: capabilityTaskNames.speech,
  transcribe: capabilityTaskNames.transcribe,
  music: capabilityTaskNames.music,
  sound_effects: capabilityTaskNames.sfx,
  audio: capabilityTaskNames.audio,
  decisions: capabilityTaskNames.decide,
  embeddings: capabilityTaskNames.embed,
  reranking: capabilityTaskNames.rerank,
} as const;

/**
 * Capabilities apps use behind the scenes (document search, memory, routing),
 * not tasks a member does: the Try plan keeps "Choose an option" and "Turn
 * text into numbers" off member task pages. Member status summaries (Home,
 * Status, the shell indicator) leave them out; owners and builders still see
 * them. Keys as the gateway's capability rows name them.
 */
export const appPlumbingCapabilities: ReadonlySet<string> = new Set([
  "embed",
  "embeddings",
  "decide",
  "decisions",
  "rerank",
  "reranking",
]);

/** Member words for screens that list what a person can use. */
export const memberTerms = {
  /** A member's list of what works for them (not "Your capabilities"). */
  whatYouCanUse: "What you can use",
  /** One kind of request on Usage (not "capability"). */
  task: "task",
} as const;

/** Preferred member-facing terms; protocol and stored field names stay unchanged. */
export const preferredTerms = {
  localExecution: "Engine",
  remoteExecution: "Provider",
  clientConnection: "App",
  requestCredential: "Key",
  administrativeCredential: "Admin key",
  permissionSet: "Access plan",
  person: "Person",
  authority: "Owner / Member / Admin",
  availableTask: "Capability",
  requestSelector: "Model name",
  installedTarget: "Model",
  multiStepMediaTask: "Workflow",
  request: "Request",
  durableMediaExecution: "Job",
  requestRecord: "Receipt",
  administrativeHistory: "Activity → Changes",
  remoteCharges: "Remote spend (USD)",
  localPricing: "Local · no charge",
  connectionAddress: "Gateway address",
  requestTest: "Gateway check",
  providerProbe: "Check provider connection",
  details: "Details",
} as const;
