import type { WorkflowInput } from "./workflow-inputs";

/** The catalog capability of the TRELLIS.2 workflow: its row lists under "mesh", not in Workflow. */
export const MESH_CAPABILITY = "mesh";
/** The gateway route the mesh workflow posts to. */
export const MESH_PATH = "/v1/meshes/generate";
export const MESH_MEDIA_TYPE = "model/gltf-binary";
export const MESH_WAIT_MESSAGE = "Making the 3D model. This can take about a minute.";
export const MESH_PICTURE_TYPES = ["image/png", "image/jpeg", "image/webp"];
/** The seed is a 32-bit unsigned integer, as the gateway's manifest says. */
const MAX_SEED = 4294967295;

export type MeshDetail = { value: number; label: string };
export type MeshBackground = "" | "threshold" | "birefnet";

/**
 * The "detail" pick-list: the manifest's resolution values, with the manifest's default selected.
 * Empty when the manifest carries no resolution input, so the mode has nothing to offer.
 */
export function meshDetailChoices(inputs: WorkflowInput[]): { choices: MeshDetail[]; defaultValue: number | null } {
  const resolution = inputs.find((input) => input.name === "resolution");
  const values = (resolution?.enum ?? []).filter((value): value is number => typeof value === "number");
  const fallback = typeof resolution?.default === "number" ? resolution.default : null;
  return {
    choices: values.map((value) => ({ value, label: String(value) })),
    defaultValue: fallback !== null && values.includes(fallback) ? fallback : (values[0] ?? null),
  };
}

/** A client-side check of the seed box, so a bad number never leaves the page. Empty means "let the service choose". */
export function meshSeedProblem(seed: string): string | null {
  const text = seed.trim();
  if (!text) return null;
  const value = Number(text);
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(value) || value > MAX_SEED) {
    return `Enter a whole number from 0 to ${MAX_SEED}, or leave it empty.`;
  }
  return null;
}

/** The multipart body the route takes. Empty seed and background are left out, so the service chooses. */
export function meshForm(options: { image: File; seed: string; resolution: number; background: MeshBackground }): FormData {
  const form = new FormData();
  form.append("image", options.image);
  form.append("resolution", String(options.resolution));
  if (options.seed.trim()) form.append("seed", options.seed.trim());
  if (options.background) form.append("bg_removal", options.background);
  return form;
}

/** A download name that sorts by time and has no characters a file system refuses. */
export function meshDownloadName(now: Date): string {
  const stamp = now.toISOString().slice(0, 19).replace(/:/g, "-");
  return `azizam-3d-${stamp}.glb`;
}

/** The message to show for a failed mesh request: the server's own words, or a plain fallback when it sent none. */
export function meshFailureMessage(payload: unknown): string {
  const message = (payload as { error?: { message?: unknown } } | null)?.error?.message;
  return typeof message === "string" && message.trim()
    ? message
    : "The 3D model could not be made. Try again.";
}
