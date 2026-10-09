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
  transcribe: "Turn a recording into text",
  music: "Make music",
  sound_effects: "Make a sound effect",
  audio: "Make music or sounds",
  decisions: "Choose an option",
  embeddings: "Turn text into numbers",
  reranking: "Reranking",
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
