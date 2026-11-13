import type { HandoffFileSlot } from "./handoff-files";
import {
  workflowAvailabilityView,
  type WorkflowAvailability,
} from "@/lib/workflow-availability";
import type { WorkflowInput } from "./workflow-inputs";

/** The catalog capability of the 3D workflows. */
export const MESH_CAPABILITY = "mesh";

/**
 * Whether a workflow belongs in Photo to 3D: a 3D workflow whose required files are all pictures, at least one. A 3D
 * stage that works on a model file (an edit stage, also one that takes a reference picture beside the model) lists under
 * Workflow instead and never decides whether the Photo to 3D tab is shown.
 */
export function makesAModelFromAPicture(workflow: { capability?: string; inputs: WorkflowInput[] } | undefined): boolean {
  if (workflow?.capability !== MESH_CAPABILITY) return false;
  const files = workflow.inputs.filter((input) => input.asset === true && input.required === true);
  const isPicture = (input: WorkflowInput) =>
    [input.media_type, ...(input.accepted_media_types ?? [])].some(
      (type) => typeof type === "string" && type.startsWith("image/"),
    );
  return files.length > 0 && files.every(isPicture);
}

/**
 * Photo to 3D's workflows in the data's order: the lowest `selection_rank` first (the one the 3D route runs when no
 * workflow is named), then by id; an unranked workflow comes last. The first one decides the tab and its choices.
 */
export function photoTo3dOrder<T extends { id: string; workflow?: { selectionRank?: number } }>(options: T[]): T[] {
  const place = (option: T) => option.workflow?.selectionRank ?? Number.POSITIVE_INFINITY;
  return [...options].sort((a, b) => place(a) - place(b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

/** The gateway route the mesh workflow posts to. */
export const MESH_PATH = "/v1/3d/generations/glb";
export const MESH_MEDIA_TYPE = "model/gltf-binary";
/** The job routes the async form uses: poll, fetch the model, cancel. */
export const MESH_JOB_PATH = "/gateway/v1/media/jobs/";
export const MESH_WAIT_MESSAGE =
  "Making the 3D model. This can take several minutes. You can leave this page: the model also appears in your Library when it is done.";
export const MESH_RECONNECTING_MESSAGE = "Reconnecting…";
export const MESH_CANCELLING_MESSAGE =
  "Cancelling. The 3D service finishes the run it has started first.";
export const MESH_STOPPED_MESSAGE =
  "You stopped waiting. The model keeps running and also appears in your Library when it is done.";
export const MESH_UNREACHABLE_MESSAGE =
  "Azizam is not answering. Your 3D model may still be running, and it appears in your Library when it is done. Your place is kept.";
export const MESH_CANCELED_MESSAGE = "The 3D model was canceled.";
/** Where the running job's id waits, so a reload or an app switch on a phone picks the job up again. */
export const MESH_JOB_STORAGE_KEY = "azizam.try.mesh-job";
export const MESH_BUSY_MESSAGE =
  "The GPU is busy or starting up. Try again shortly.";
export const MESH_PICTURE_TYPES = ["image/png", "image/jpeg", "image/webp"];
/** The file input this panel declares, for the files Home hands to Try (see assignHandoffFiles). */
export const MESH_FILE_SLOTS: HandoffFileSlot[] = [{ inputName: "image", required: true, family: "image" }];
/** The seed is a 32-bit unsigned integer, as the gateway's manifest says. */
const MAX_SEED = 4294967295;

export type MeshDetail = { value: number; label: string };
export type MeshBackground = "" | "threshold" | "birefnet";

/**
 * The "detail" pick-list: the manifest's resolution values, with the manifest's default selected.
 * Empty when the manifest carries no resolution input, so the mode has nothing to offer.
 */
export function meshDetailChoices(inputs: WorkflowInput[]): {
  choices: MeshDetail[];
  defaultValue: number | null;
} {
  const resolution = inputs.find((input) => input.name === "resolution");
  const values = (resolution?.enum ?? []).filter(
    (value): value is number => typeof value === "number",
  );
  const fallback =
    typeof resolution?.default === "number" ? resolution.default : null;
  return {
    choices: values.map((value) => ({ value, label: String(value) })),
    defaultValue:
      fallback !== null && values.includes(fallback)
        ? fallback
        : (values[0] ?? null),
  };
}

/**
 * Whether the Photo to 3D mode is offered. "ready": it runs now. "busy": its GPU is busy or starting, which passes, so
 * the mode stays and says so. "hidden": the capability is missing or its service is not healthy.
 */
export type MeshModeState = "ready" | "busy" | "hidden";

const TEMPORARY_STATUSES = new Set(["busy", "starting"]);

export function meshModeState(
  availability: WorkflowAvailability | null | undefined,
  options: { owner: boolean; engineBinding?: string | null },
): MeshModeState {
  const status = String(availability?.status ?? "").toLowerCase();
  if (TEMPORARY_STATUSES.has(status)) return "busy";
  return workflowAvailabilityView(availability, options).runnable
    ? "ready"
    : "hidden";
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
export function meshForm(options: {
  image: File;
  seed: string;
  resolution: number;
  background: MeshBackground;
}): FormData {
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
  const message = (payload as { error?: { message?: unknown } } | null)?.error
    ?.message;
  return typeof message === "string" && message.trim()
    ? message
    : "The 3D model could not be made. Try again.";
}

/** The wait between two polls: 2 s at first, backing off to 5 s. */
export function meshPollDelayMs(attempt: number): number {
  return Math.min(5000, 2000 + Math.max(0, attempt) * 500);
}

/** How long requests may keep failing before the panel stops saying "Reconnecting…" and offers "Check again". */
export function meshUnreachableAfterMs(): number {
  return 120_000;
}

/** Elapsed time as m:ss. */
export function meshElapsed(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  return `${Math.floor(total / 60)}:${seconds}`;
}

/** The job a submit answered with: its id and, if the gateway sent one, its status. Null when the body is no job. */
export function meshJobFrom(
  payload: unknown,
): { id: string; status: string } | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  const id = record.id ?? record.job_id;
  if (typeof id !== "string" || !id) return null;
  return { id, status: typeof record.status === "string" ? record.status : "queued" };
}

/** The job the page was following when it was left, or null. */
export type StoredMeshJob = { id: string; startedAt: number; picture: string };

export function readStoredMeshJob(storage: Storage): StoredMeshJob | null {
  try {
    const raw: unknown = JSON.parse(storage.getItem(MESH_JOB_STORAGE_KEY) ?? "null");
    const record = raw as Partial<StoredMeshJob> | null;
    if (
      record &&
      typeof record.id === "string" &&
      record.id &&
      typeof record.startedAt === "number" &&
      typeof record.picture === "string"
    ) {
      return { id: record.id, startedAt: record.startedAt, picture: record.picture };
    }
  } catch {
    // An unreadable record is no record.
  }
  return null;
}

export function writeStoredMeshJob(storage: Storage, job: StoredMeshJob | null) {
  try {
    if (job) storage.setItem(MESH_JOB_STORAGE_KEY, JSON.stringify(job));
    else storage.removeItem(MESH_JOB_STORAGE_KEY);
  } catch {
    // Storage can be full or blocked; the job still runs, it is only not resumed after a reload.
  }
}
