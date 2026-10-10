/** Shared presentation vocabulary for gateway capabilities and product concepts. */
export const capabilityLabels = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  speech: "Speech",
  transcribe: "Transcription",
  music: "Music",
  sound_effects: "Sound effects",
  audio: "Audio",
  decisions: "Decisions",
  embeddings: "Embeddings",
  reranking: "Reranking",
} as const;

/**
 * What a person asks for, in the words Home and Try use. Screens that name a
 * kind of request (Usage rows, chart legends) use these, so one thing keeps
 * one name across the app.
 */
export const taskNames = {
  chat: "Chat",
  image: "Make a picture",
  video: "Make a video",
  speech: "Read text aloud",
  transcribe: "Transcribe a recording",
  music: "Make music",
  sound_effects: "Make a sound effect",
  audio: "Make music or sounds",
  decisions: "Choose an option",
  embeddings: "Turn text into numbers",
  reranking: "Sort search results",
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
  localPricing: "Local · no provider charge",
  connectionAddress: "Gateway address",
  requestTest: "Gateway check",
  providerProbe: "Check provider connection",
  details: "Details",
} as const;
