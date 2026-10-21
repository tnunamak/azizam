"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useSearchParams } from "next/navigation";
import {
  AudioLines,
  MessageSquare,
  Copy,
  EyeOff,
  GitBranch,
  History,
  Image as ImageIcon,
  Loader2,
  Mic,
  Pause,
  Play,
  Layers,
} from "lucide-react";
import {
  WorkflowInputs,
  fileFieldOf,
  isFileInput,
  parseWorkflowInputs,
  workflowDefaults,
  workflowInputProblems,
  type WorkflowInput,
  type WorkflowInputMapping,
} from "./workflow-inputs";
import {
  mediaFrom,
  workflowJob,
  type WorkflowMedia,
  type WorkflowJob,
} from "./workflow-media";
import { outputKindOf } from "./workflow-input-meta";
import { costFrom } from "./result-cost";
import { runImageJob } from "./image-job";
import { isNetworkFailure, networkFetch } from "@/lib/network-error";
import { ReceiptLink } from "./receipt-link";
import { VoicePicker, selectedVoice, type SpeechVoices } from "./voice-picker";
import { ServiceNotice, serviceUnavailable } from "@/components/product/service-notice";
import { accountApi } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import {
  workflowAvailabilityView,
  type WorkflowAvailability,
} from "@/lib/workflow-availability";
import { useViewer } from "@/components/shell/session";
import Link from "next/link";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/cn";
import {
  tryKeepState,
  withForget,
  type MemberPromptMemory,
  type TryKeepState,
} from "@/lib/prompt-memory";
import { taskNames } from "@/lib/terms";
import { modelDisplayName } from "@/lib/model-name";
import { Button } from "@/components/ui/button";
import { useCopied } from "@/components/ui/copy-button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader, EmptyState, ErrorState } from "@/components/product";

type TryTab =
  | "chat"
  | "image"
  | "speech"
  | "transcribe"
  | "decide"
  | "embed"
  | "workflow";
type TryKey = { id: string; secret: string; appName: string };
type TryError = {
  message: string;
  /** Replaces "<tab> request failed" when the request did not fail. */
  heading?: string;
  /** The connection dropped; the gateway may still have finished. */
  lost?: boolean;
  /** Offer the Library, where a finished creation appears. */
  library?: boolean;
  /** Where the request stopped, when the gateway said (P3). */
  layer?: string;
  receiptId?: string | null;
  /** HTTP status, time and the raw answer, for Technical details. */
  technical?: string;
};
type RequestInfo = {
  path: string;
  body: unknown;
  multipart?: boolean;
  form?: Record<string, unknown>;
  files?: WorkflowExampleFile[];
};
type WorkflowExampleFile = {
  inputName: string;
  field: string;
  /** A made-up local path for the code examples only; never helper text. */
  examplePath: string;
  type?: string;
  required?: boolean;
  selected?: boolean;
};
type CatalogOption = {
  id: string;
  label: string;
  recommended?: boolean;
  capability: TryTab;
  price: string;
  locality: string;
  provider?: string;
  workflow?: WorkflowOption;
  speechVoices?: SpeechVoices;
  unavailableMessage?: string;
  /** Other ids that name the same model (an alias and its target). */
  alsoKnownAs?: string[];
};
type WorkflowOption = {
  id: string;
  short_id?: string;
  title: string;
  description?: string;
  /** The catalog's declared capability: "video", "music", "sfx", ... */
  capability?: string;
  /** Other names the gateway accepts for this workflow. */
  aliases?: string[];
  availability?: WorkflowAvailability;
  inputs: WorkflowInput[];
  inputMapping?: WorkflowInputMapping;
  jobRoutes?: { poll?: { path?: string }; content?: { path?: string } };
  outputs?: Array<{ kind?: string; type?: string; media_type?: string }>;
  engine_binding?: string;
  native_route?: string | null;
  example_request?: {
    method?: string;
    path?: string;
    content_type?: string;
    body?: Record<string, unknown>;
    form?: Record<string, unknown>;
    curl?: string;
    python?: string;
    javascript?: string;
  };
};
type CatalogState = "idle" | "loading" | "ready" | "empty" | "error";
type CatalogSection = "models" | "workflows";

type CatalogGroups = Partial<Record<TryTab, CatalogOption[]>>;
type Result = {
  title: string;
  body: string;
  answerNote?: string;
  copyAnswer?: boolean;
  runId?: number;
  elapsedMs: number;
  cost?: string;
  receiptId: string | null;
  /** The receipt could not be checked; this is what to check again. */
  receiptLookup?: ReceiptLookup;
  checkingReceipt?: boolean;
  /** What the gateway did not return ("reply", "picture"), when it was missing. */
  missing?: string;
  request: RequestInfo;
  imageUrl?: string;
  imageModel?: string;
  audioUrl?: string;
  downloadUrl?: string;
  downloadName?: string;
  vector?: { dimensions: number; min: number; max: number; mean: number };
  media?: WorkflowMedia[];
  job?: WorkflowJob;
  technical?: string;
  probabilities?: Array<{ option: string; probability: number | null }>;
  /** The run asked the gateway not to keep its words, or memory was off. */
  promptNotSaved?: boolean;
};

/** Tabs whose words the gateway can keep with what they make. */
const MEMORY_TABS = new Set<TryTab>(["image", "speech", "workflow"]);
/** "Don't keep this prompt", remembered for the browser session. */
const FORGET_SESSION_KEY = "azizam.try.forget";
/** Remove secrets persisted by older versions; new keys stay in page memory. */
const TRY_KEY_STORAGE_KEY = "azizam.try.key";

function forgetStoredTryKey() {
  try {
    localStorage.removeItem(TRY_KEY_STORAGE_KEY);
  } catch {
    // Storage may be blocked; never fall back to persisting a secret.
  }
}

const DEFAULTS: Record<TryTab, { model: string; prompt: string }> = {
  chat: { model: "", prompt: "Reply with one short sentence." },
  image: { model: "", prompt: "A calm teal fish mark on a white card." },
  speech: { model: "", prompt: "The gateway is ready." },
  transcribe: { model: "", prompt: "" },
  // Everyday examples: a member reads these, so no gateway test words.
  decide: {
    model: "",
    prompt: "What should I do?",
  },
  embed: {
    model: "",
    prompt: "Pancakes need flour, eggs and milk.",
  },
  workflow: {
    model: "",
    prompt: "{}",
  },
};

const TAB_ITEMS: Array<{
  value: TryTab;
  label: string;
  icon: typeof MessageSquare;
  help: string;
}> = [
  {
    value: "chat",
    label: taskNames.chat,
    icon: MessageSquare,
    help: "Send a message and get a reply.",
  },
  {
    value: "image",
    label: taskNames.image,
    icon: ImageIcon,
    help: "Describe the picture you want to make.",
  },
  {
    value: "speech",
    label: taskNames.speech,
    icon: AudioLines,
    help: "Enter text to hear it spoken aloud.",
  },
  {
    value: "transcribe",
    label: taskNames.transcribe,
    icon: Mic,
    help: "Upload a recording or record your voice to get the words as text.",
  },
  {
    value: "decide",
    label: taskNames.decisions,
    icon: GitBranch,
    help: "Provide information to help pick one of the options.",
  },
  {
    value: "embed",
    label: taskNames.embeddings,
    icon: Layers,
    help: "Turn text into numbers that search tools can use.",
  },
  {
    value: "workflow",
    label: "Workflow",
    icon: GitBranch,
    help: "Run a workflow with its required inputs.",
  },
];

const INTENT_TABS: Record<string, TryTab> = {
  "chat.completions": "chat",
  "image.generate": "image",
  "speech.synthesis": "speech",
  "speech.transcription": "transcribe",
  "audio.transcription": "transcribe",
  "text.embeddings": "embed",
  "decision.choice": "decide",
  "decision.systemone": "decide",
  systemone: "decide",
};
const CAPABILITY_TABS: Record<string, TryTab> = {
  chat: "chat",
  image: "image",
  images: "image",
  speech: "speech",
  voice: "speech",
  tts: "speech",
  transcribe: "transcribe",
  transcription: "transcribe",
  stt: "transcribe",
  decide: "decide",
  decision: "decide",
  embed: "embed",
  embeddings: "embed",
  workflow: "workflow",
};

/**
 * Kinds of work Try runs through a workflow. A request for one opens
 * Workflow with only the workflows whose catalog capability matches. The
 * keys are the capability vocabulary, not engine or workflow names.
 */
type RequestedKind = {
  /** Lower case, for sentences: "No video is available to you right now." */
  noun: string;
  /** The noun takes "are": "No sound effects are available". */
  plural?: boolean;
  /** Catalog workflow capabilities that make this kind. */
  capabilities: string[];
  /** Try has no way to run this kind at all. */
  untryable?: boolean;
};
const REQUESTED_KINDS: Record<string, RequestedKind> = {
  video: { noun: "video", capabilities: ["video"] },
  music: { noun: "music", capabilities: ["music"] },
  sfx: { noun: "sound effects", plural: true, capabilities: ["sfx"] },
  sound_effects: { noun: "sound effects", plural: true, capabilities: ["sfx"] },
  audio: { noun: "audio", capabilities: ["music", "sfx", "audio"] },
  rerank: { noun: "reranking", capabilities: [], untryable: true },
};

function requestedTabFor(key: string | undefined): TryTab | null {
  if (!key) return null;
  if (CAPABILITY_TABS[key]) return CAPABILITY_TABS[key];
  const kind = REQUESTED_KINDS[key];
  return kind && !kind.untryable ? "workflow" : null;
}

function workflowMakes(option: CatalogOption, kind: RequestedKind) {
  const capability = option.workflow?.capability;
  return Boolean(capability && kind.capabilities.includes(capability));
}

/** What the model picker is called in the "none available" line. */
const EMPTY_NOUN: Record<TryTab, string> = {
  chat: "chat models",
  image: "picture models",
  speech: "voice models",
  transcribe: "transcription models",
  decide: "decision models",
  embed: "embedding models",
  workflow: "workflows",
};

function baseUrl() {
  if (typeof window === "undefined") return "";
  return `${window.location.origin}/openai/v1`;
}

function publicPath(path: string) {
  if (path.startsWith("/gateway/")) return path;
  return path.startsWith("/v1/") ? path : `/openai/v1${path}`;
}

class PublicRequestError extends ApiError {
  constructor(
    message: string,
    public response: Response,
    public payload: unknown,
    public elapsedMs: number,
  ) {
    super("public_request_failed", message, response.status);
  }

  /** The layer the gateway named in the error's details, if any. */
  get layer() {
    const layer = (
      this.payload as { error?: { details?: { layer?: unknown } } } | null
    )?.error?.details?.layer;
    return typeof layer === "string" && layer in LAYER_HEADINGS
      ? layer
      : undefined;
  }
}

/**
 * A failure's heading names the layer that stopped it (P3). The layers are
 * the gateway's public failure vocabulary; "unknown" has no heading of its
 * own, so the card keeps "<task> request failed".
 */
const LAYER_HEADINGS: Record<string, string> = Object.fromEntries([
  ["request", "The request wasn’t accepted"],
  ["account", "Your account stopped this request"],
  ["library", "A Library item isn’t available"],
  ["gateway", "The gateway stopped this request"],
  ["engine", "The engine stopped this request"],
  // Pairs, not an object literal: the model/voice guard reads `model: "…"`
  // as a hard-coded model id.
  ["model", "The model stopped this request"],
  ["workflow", "The workflow stopped this request"],
]);

/** Said when nothing shows where a request stopped. */
const UNKNOWN_FAILURE = "We couldn’t tell where this request stopped.";

/** The connection dropped and the gateway may still have finished the work. */
class LostRequestError extends Error {
  constructor(
    public tab: TryTab,
    public jobId?: string,
  ) {
    super("The connection dropped before the answer came back.");
  }
}

const IMAGE_JOB_PATH = "/gateway/v1/media/jobs/image-generations";

function lostCopy(
  tab: TryTab,
): Pick<TryError, "heading" | "message" | "library"> {
  if (tab === "image")
    return {
      heading: "Your picture may still be finishing",
      message:
        "The connection dropped, but the gateway may still be making it. If it finishes, it will appear in your Library.",
      library: true,
    };
  if (tab === "speech" || tab === "workflow")
    return {
      heading: `Your ${tab === "speech" ? "audio" : "result"} may still be finishing`,
      message:
        "The connection dropped, but the gateway may still be working. If it finishes, it will appear in your Library.",
      library: true,
    };
  return {
    heading: "The connection dropped",
    message:
      "The answer did not come back. Edit and run again when you are back online.",
  };
}

function newIdempotencyKey() {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `try-${random}`;
}

function createdAtSeconds(value: unknown) {
  if (typeof value === "number") return value > 1e12 ? value / 1000 : value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed / 1000;
  }
  return null;
}

/**
 * After a lost connection, the picture may already be in the Library. Look
 * for the newest picture from this job, or made for this prompt, since the
 * run began. A prompt that was not kept has no prompt to match, so the job
 * is the better key.
 */
async function recentCreation(
  prompt: unknown,
  startedAtMs: number,
  jobId?: string,
) {
  if ((typeof prompt !== "string" || !prompt) && !jobId) return null;
  try {
    const response = await fetch("/me/api/media?kind=image&limit=5&scope=me", {
      credentials: "same-origin",
    });
    if (!response.ok) return null;
    const payload = (await response.json()) as {
      assets?: Array<{
        asset_id?: string;
        kind?: string;
        prompt?: string | null;
        job_id?: string | null;
        created_at?: unknown;
        available?: boolean;
      }>;
    };
    const found = (payload.assets ?? []).find((asset) => {
      const created = createdAtSeconds(asset.created_at);
      return (
        asset.asset_id &&
        asset.available !== false &&
        (asset.kind ?? "image") === "image" &&
        ((jobId && asset.job_id === jobId) ||
          (typeof prompt === "string" && prompt && asset.prompt === prompt)) &&
        created !== null &&
        created >= startedAtMs / 1000 - 5
      );
    });
    return found?.asset_id ?? null;
  } catch {
    return null;
  }
}

async function publicRequest(path: string, key: string, init: RequestInit) {
  const started = performance.now();
  const response = await networkFetch(publicPath(path), {
    ...init,
    headers: { authorization: `Bearer ${key}`, ...(init.headers ?? {}) },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch((error: unknown) => {
        if (isNetworkFailure(error)) throw error;
        if (!response.ok) return null;
        throw new Error("The gateway sent a reply we could not read. Try again.");
      })
    : await response.blob();
  const elapsedMs = Math.round(performance.now() - started);
  if (!response.ok) {
    const message =
      typeof payload === "object" &&
      payload !== null &&
      "error" in payload &&
      typeof (payload as { error?: { message?: unknown } }).error?.message ===
        "string"
        ? (payload as { error: { message: string } }).error.message
        : UNKNOWN_FAILURE;
    throw new PublicRequestError(message, response, payload, elapsedMs);
  }
  return { response, payload, elapsedMs };
}

function receiptFrom(response: Response, payload: unknown) {
  const header =
    response.headers.get("x-azizam-receipt-id") ??
    response.headers.get("x-azizam-request-id") ??
    response.headers.get("x-azizam-request-id") ??
    response.headers.get("x-request-id");
  if (header) return header;
  if (typeof payload === "object" && payload !== null) {
    const requestId = (payload as Record<string, unknown>).request_id;
    if (typeof requestId === "string") return requestId;
  }
  return null;
}

/** Where a run's receipt can be found: its request id, or its creations. */
type ReceiptLookup = { requestId: string | null; assetIds: string[] };

function receiptLookup(
  response: Response,
  payload: unknown,
  hint?: string | null,
): ReceiptLookup {
  // Media routes can return asset handles without a request-id header. The
  // account-scoped asset record carries the exact generation's provenance.
  const record = payload as {
    data?: Array<{ asset_id?: unknown }>;
    assets?: Array<{ asset_id?: unknown }>;
  } | null;
  const items = [
    ...(Array.isArray(record?.data) ? record.data : []),
    ...(Array.isArray(record?.assets) ? record.assets : []),
  ];
  return {
    requestId: receiptFrom(response, payload) ?? hint ?? null,
    assetIds: [
      ...new Set(
        items
          .map((item) => item?.asset_id)
          .filter((id): id is string => typeof id === "string" && id.length > 0),
      ),
    ].slice(0, 10),
  };
}

/**
 * Finds the run's receipt. "No receipt" and "could not check" are different
 * answers (PF-008): a missing receipt is a fact, a failed check is retried.
 */
async function checkReceipt(
  lookup: ReceiptLookup,
): Promise<{ receiptId: string | null; receiptLookup?: ReceiptLookup }> {
  let failed = false;
  /** The id when its receipt exists; null when it does not. Throws on failure. */
  async function verify(requestId: string | null) {
    if (!requestId) return null;
    const receipt = await fetch(
      `/me/api/receipts/${encodeURIComponent(requestId)}`,
      { credentials: "same-origin" },
    );
    if (receipt.ok) return requestId;
    if (receipt.status === 404) return null;
    throw new Error(`Receipt check returned HTTP ${receipt.status}`);
  }
  try {
    const direct = await verify(lookup.requestId);
    if (direct) return { receiptId: direct };
  } catch {
    failed = true;
  }
  for (const id of lookup.assetIds) {
    try {
      const detail = await fetch(`/me/api/media/${encodeURIComponent(id)}`, {
        credentials: "same-origin",
      });
      if (!detail.ok) {
        if (detail.status !== 404) failed = true;
        continue;
      }
      const value = (await detail.json()) as {
        asset?: { request_id?: unknown };
      };
      const requestId =
        typeof value.asset?.request_id === "string"
          ? value.asset.request_id
          : null;
      const verified = await verify(requestId);
      if (verified) return { receiptId: verified };
    } catch {
      failed = true;
    }
  }
  return failed ? { receiptId: null, receiptLookup: lookup } : { receiptId: null };
}

/** Bound receipt waiting even when a fetch does not settle. */
async function timedReceipt(lookup: ReceiptLookup) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      checkReceipt(lookup),
      new Promise<{ receiptId: string | null; receiptLookup: ReceiptLookup }>((resolve) => {
        timer = setTimeout(() => resolve({ receiptId: null, receiptLookup: lookup }), 10_000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/** A media job's state in plain words (PF-006); the raw status is technical. */
function jobPhase(status: string) {
  const value = status.toLowerCase();
  if (["queued", "pending", "waiting", "submitted"].includes(value))
    return "Waiting to start";
  if (["running", "processing", "in_progress", "started"].includes(value))
    return "Being made";
  if (["succeeded", "completed"].includes(value)) return "Ready";
  if (["failed", "cancelled", "canceled", "expired", "error"].includes(value))
    return "Couldn’t finish";
  return "The gateway sent a state Try doesn’t know";
}

const FINISHED_JOB = ["succeeded", "completed"];
const ENDED_JOB = [...FINISHED_JOB, "failed", "cancelled", "canceled", "expired", "error"];

function codeSnippets(request: RequestInfo) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const url =
    request.path.startsWith("/v1/") || request.path.startsWith("/gateway/")
      ? `${origin}${request.path}`
      : `${baseUrl()}${request.path}`;
  const model = (request.body as { model?: string }).model ?? "";
  const jsonBody = JSON.stringify(request.body, null, 2);
  if (request.multipart) {
    const form = request.form ?? { model };
    const files: WorkflowExampleFile[] = request.files?.filter(
      (file) => file.required !== false || file.selected,
    ) ?? [{ inputName: "file", field: "file", examplePath: "sample.wav" }];
    const fields = Object.entries(form)
      .filter(([, value]) => value !== undefined && value !== null)
      .map(([name, value]) => [
        name,
        typeof value === "object" ? JSON.stringify(value) : String(value),
      ]);
    const shellQuote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
    const curlFields = fields.map(
      ([name, value]) => `  --form-string ${shellQuote(`${name}=${value}`)}`,
    );
    const curlFiles = files.map(
      (file) =>
        `  -F ${shellQuote(`${file.field}=@${file.examplePath}${file.type ? `;type=${file.type}` : ""}`)}`,
    );
    const pythonFiles = files
      .map(
        (file) =>
          `    (${JSON.stringify(file.field)}, (${JSON.stringify(file.examplePath)}, stack.enter_context(open(${JSON.stringify(file.examplePath)}, "rb")), ${JSON.stringify(file.type ?? "application/octet-stream")})),`,
      )
      .join("\n");
    const jsFields = fields
      .map(
        ([name, value]) =>
          `form.set(${JSON.stringify(name)}, ${JSON.stringify(value)});`,
      )
      .join("\n");
    const jsFiles = files
      .map(
        (file) =>
          `form.append(${JSON.stringify(file.field)}, new Blob([await fs.readFile(${JSON.stringify(file.examplePath)})], {type: ${JSON.stringify(file.type ?? "application/octet-stream")}}), ${JSON.stringify(file.examplePath)});`,
      )
      .join("\n");
    return [
      {
        language: "curl",
        code: [
          `curl ${shellQuote(url)}`,
          '  -H "Authorization: Bearer $GATEWAY_API_KEY"',
          ...curlFields,
          ...curlFiles,
        ].join(" \\\n"),
      },
      {
        language: "Python",
        code: `import os\nimport json\nimport requests\nfrom contextlib import ExitStack\n\nwith ExitStack() as stack:\n    files = [\n${pythonFiles}\n    ]\n    data = json.loads(${JSON.stringify(JSON.stringify(Object.fromEntries(fields)))})\n    response = requests.post(${JSON.stringify(url)}, headers={"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}, data=data, files=files)\n    print(response.status_code)\n    open("workflow-result", "wb").write(response.content)`,
      },
      {
        language: "JavaScript",
        code: `import fs from "node:fs/promises";\n\nconst form = new FormData();\n${jsFields}\n${jsFiles}\nconst response = await fetch(${JSON.stringify(url)}, {method: "POST", headers: {authorization: \`Bearer \${process.env.GATEWAY_API_KEY}\`}, body: form});\nconsole.log(response.status);\nawait fs.writeFile("workflow-result", Buffer.from(await response.arrayBuffer()));`,
      },
    ];
  }
  if (request.path === "/audio/speech") {
    return [
      {
        language: "curl",
        code: `curl '${url}' \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '${jsonBody.replaceAll("'", "'\\''")}' \\\n  --output speech.mp3`,
      },
      {
        language: "Python",
        code: `import os\nimport json\nimport requests\n\npayload = json.loads(${JSON.stringify(jsonBody)})\nresponse = requests.post(\n    "${url}",\n    headers={"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"},\n    json=payload,\n)\nopen("speech.mp3", "wb").write(response.content)`,
      },
      {
        language: "JavaScript",
        code: `import fs from "node:fs/promises";\n\nconst response = await fetch("${url}", {\n  method: "POST",\n  headers: {\n    authorization: \`Bearer \${process.env.GATEWAY_API_KEY}\`,\n    "content-type": "application/json",\n  },\n  body: JSON.stringify(${jsonBody}),\n});\nawait fs.writeFile("speech.mp3", Buffer.from(await response.arrayBuffer()));`,
      },
    ];
  }
  return [
    {
      language: "curl",
      code: `curl '${url}' \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d '${jsonBody.replaceAll("'", "'\\''")}'`,
    },
    {
      language: "Python",
      code: `import os\nimport json\nimport requests\n\npayload = json.loads(${JSON.stringify(jsonBody)})\nresponse = requests.post(\n    "${url}",\n    headers={"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"},\n    json=payload,\n)\nprint(response.json())`,
    },
    {
      language: "JavaScript",
      code: `const response = await fetch("${url}", {\n  method: "POST",\n  headers: {\n    authorization: \`Bearer \${process.env.GATEWAY_API_KEY}\`,\n    "content-type": "application/json",\n  },\n  body: JSON.stringify(${jsonBody}),\n});\nconsole.log(await response.json());`,
    },
  ];
}

function textFromChat(payload: unknown) {
  const choice = (
    payload as { choices?: Array<{ message?: { content?: string } }> }
  )?.choices?.[0];
  const content = choice?.message?.content;
  return typeof content === "string" && content.trim() ? content : null;
}

function imageFrom(payload: unknown) {
  const item = (
    payload as { data?: Array<{ url?: string; b64_json?: string }> }
  )?.data?.[0];
  if (item?.url) return item.url;
  if (item?.b64_json) return `data:image/png;base64,${item.b64_json}`;
  return undefined;
}

function embeddingStats(payload: unknown) {
  const values = (payload as { data?: Array<{ embedding?: number[] }> })
    ?.data?.[0]?.embedding;
  if (!Array.isArray(values) || !values.length) return null;
  const sum = values.reduce((total, value) => total + value, 0);
  return {
    dimensions: values.length,
    min: Math.min(...values),
    max: Math.max(...values),
    mean: sum / values.length,
  };
}

function decisionRows(payload: unknown, options: string[]) {
  const record = payload as Record<string, unknown>;
  const answer =
    (record.answers as { route?: Record<string, unknown> } | undefined)
      ?.route ?? (record.answer as Record<string, unknown> | undefined);
  const probabilities =
    answer?.probabilities ??
    record.probabilities ??
    record.probs ??
    (record.decision as { probabilities?: unknown } | undefined)?.probabilities;
  if (probabilities && typeof probabilities === "object") {
    return options.map((option) => {
      const value = (probabilities as Record<string, unknown>)[option];
      return { option, probability: typeof value === "number" ? value : null };
    });
  }
  return options.map((option) => ({ option, probability: null }));
}

function groupCatalog(payload: unknown) {
  if (
    !payload ||
    typeof payload !== "object" ||
    !Array.isArray((payload as { aliases?: unknown }).aliases) ||
    !Array.isArray((payload as { models?: unknown }).models)
  ) {
    throw new Error("Invalid model catalog: expected aliases and models arrays");
  }
  const records = [
    ...(payload as { aliases: unknown[] }).aliases,
    ...(payload as { models: unknown[] }).models,
  ];
  const grouped: Partial<Record<TryTab, CatalogOption[]>> = {};
  // A stable alias ("image") and the model it points at ("krea2-turbo") are
  // one choice, not two. The alias stays; the model's id still selects it.
  const aliasTargets: Partial<Record<TryTab, Map<string, CatalogOption>>> = {};
  for (const value of records) {
    if (typeof value !== "object" || value === null) continue;
    const record = value as Record<string, unknown>;
    const id =
      stringValue(record.id) ??
      stringValue((record.use as { model?: unknown } | undefined)?.model) ??
      stringValue(record.model) ??
      stringValue(record.name);
    if (!id) continue;
    const capabilities = capabilitiesFor(record);
    if (!capabilities.length) continue;
    for (const capability of capabilities) {
      const option: CatalogOption = {
        id,
        label: modelDisplayName(id, {
          models: [
            {
              id,
              title: stringValue(record.title) ?? stringValue(record.name),
              display_name: stringValue(record.display_name),
            },
          ],
        }),
        recommended: record.recommended === true,
        capability,
        price: costLabel(record.cost),
        locality: localityLabel(record),
        provider: stringValue(record.provider) ?? undefined,
        speechVoices: record.speech_voices as SpeechVoices | undefined,
        unavailableMessage: (record.availability as { status?: string } | undefined)?.status === "unavailable"
          ? serviceUnavailable(stringValue(record.engine_label) ?? stringValue(record.title) ?? "This model")
          : undefined,
      };
      const targets = (aliasTargets[capability] ??= new Map());
      const sameChoice = targets.get(id);
      if (sameChoice) {
        sameChoice.alsoKnownAs = [...(sameChoice.alsoKnownAs ?? []), id];
        continue;
      }
      if (!(grouped[capability] ?? []).some((existing) => existing.id === id)) {
        grouped[capability] = [...(grouped[capability] ?? []), option];
        if (record.alias === true && Array.isArray(record.use_names)) {
          for (const name of record.use_names) {
            if (typeof name === "string" && name !== id) {
              targets.set(name, option);
            }
          }
        }
      }
    }
  }
  for (const capability of Object.keys(grouped) as TryTab[]) {
    const labelCounts = new Map<string, number>();
    for (const option of grouped[capability] ?? []) {
      const label = option.label.trim().toLocaleLowerCase();
      labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
    }
    grouped[capability] = [...(grouped[capability] ?? [])]
      .sort((a, b) => {
        const rank = (option: CatalogOption) =>
          option.recommended
            ? 0
            : option.locality === "Local"
              ? 1
              : option.price.startsWith("Paid")
                ? 2
                : 3;
        return rank(a) - rank(b);
      })
      .map((option) => {
        const label = option.label.trim().toLocaleLowerCase();
        return (labelCounts.get(label) ?? 0) > 1
          ? {
              ...option,
              label: [option.label, option.provider, modelDisplayName(option.id)]
                .filter(Boolean)
                .join(" · "),
            }
          : option;
      });
  }
  return grouped;
}

function groupWorkflows(payload: unknown): CatalogOption[] {
  if (
    !payload ||
    typeof payload !== "object" ||
    !Array.isArray((payload as { workflows?: unknown }).workflows)
  ) {
    throw new Error("Invalid workflow catalog: expected a workflows array");
  }
  const options: CatalogOption[] = [];
  for (const value of (payload as { workflows: unknown[] }).workflows) {
    if (typeof value !== "object" || value === null) continue;
    const record = value as Record<string, unknown>;
    const workflow: WorkflowOption = {
      id: String(record.id ?? ""),
      short_id: stringValue(record.short_id) ?? undefined,
      title:
        stringValue(record.title) ??
        stringValue(record.display_name) ??
        String(record.id ?? ""),
      description: stringValue(record.description) ?? undefined,
      capability: stringValue(record.capability) ?? undefined,
      aliases: Array.isArray(record.aliases)
        ? record.aliases.filter((name): name is string => typeof name === "string")
        : undefined,
      availability: record.availability as WorkflowOption["availability"],
      outputs: Array.isArray(record.outputs)
        ? (record.outputs as WorkflowOption["outputs"])
        : [],
      inputs: Array.isArray(record.inputs)
        ? (record.inputs as WorkflowInput[])
        : [],
      engine_binding: stringValue(record.engine_binding) ?? undefined,
      native_route:
        record.native_route === null
          ? null
          : (stringValue(record.native_route) ?? undefined),
      example_request: workflowExampleRequest(record.example_request),
      ...workflowExecution(record),
    };
    if (!workflow.id) continue;
    options.push({
      id: workflow.id,
      label: workflowPickerLabel(workflow),
      capability: "workflow" as const,
      price: "",
      locality: "Workflow manifest",
      workflow,
    });
  }
  return options;
}

/** The picker names the state with the same words as the Workflows page. */
function workflowPickerLabel(workflow: WorkflowOption) {
  const title = workflow.title || workflow.id;
  const note = workflowAvailabilityView(workflow.availability, {
    owner: false,
  }).pickerNote;
  return note ? `${title} · ${note}` : title;
}

function workflowExecution(record: Record<string, unknown>) {
  const openapi = record.openapi as
    | { execution?: Record<string, unknown> }
    | undefined;
  const execution = openapi?.execution;
  if (!execution) return {};
  const example = record.example_request as { path?: string } | undefined;
  const routes = [execution.sync, execution.async] as Array<
    | {
        path?: string;
        submit?: { path?: string };
        fields?: WorkflowInputMapping;
        poll?: { path?: string };
        content?: { path?: string };
      }
    | undefined
  >;
  const route = routes.find(
    (route) => route && (route.path ?? route.submit?.path) === example?.path,
  );
  return {
    inputMapping: route?.fields,
    jobRoutes: route ? { poll: route.poll, content: route.content } : undefined,
  };
}

function workflowExampleRequest(
  value: unknown,
): WorkflowOption["example_request"] {
  if (typeof value !== "object" || value === null) return undefined;
  const record = value as Record<string, unknown>;
  return {
    method: stringValue(record.method) ?? undefined,
    path: stringValue(record.path) ?? undefined,
    content_type: stringValue(record.content_type) ?? undefined,
    body:
      typeof record.body === "object" && record.body !== null
        ? (record.body as Record<string, unknown>)
        : undefined,
    form:
      typeof record.form === "object" && record.form !== null
        ? (record.form as Record<string, unknown>)
        : undefined,
    curl: stringValue(record.curl) ?? undefined,
    python: stringValue(record.python) ?? undefined,
    javascript: stringValue(record.javascript) ?? undefined,
  };
}

const EXAMPLE_SUFFIX: Record<string, string> = {
  image: "png",
  video: "mp4",
  audio: "wav",
};

/**
 * The multipart file parts a workflow's request carries: one per file input,
 * by the same rule the form uses to give that input its picker, so each
 * picture has exactly one control.
 */
function workflowFileSpecs(workflow: WorkflowOption): WorkflowExampleFile[] {
  if (!requestIsMultipart(workflow)) return [];
  const specs: WorkflowExampleFile[] = [];
  for (const input of workflow.inputs) {
    if (input.ignored || input.accepted === false) continue;
    if (!isFileInput(input, workflow.inputMapping)) continue;
    const field = fileFieldOf(input, workflow.inputMapping);
    const family = input.media_type?.split("/")[0] ?? "";
    specs.push({
      inputName: input.name,
      field,
      examplePath: `./${input.name.replace(/_/g, "-")}.${EXAMPLE_SUFFIX[family] ?? "bin"}`,
      required: input.required === true,
    });
  }
  return specs;
}

function stringValue(value: unknown) {
  return typeof value === "string" && value.trim() ? value : null;
}

function capabilitiesFor(record: Record<string, unknown>): TryTab[] {
  const capabilityIntents = Array.isArray(record.capabilities)
    ? record.capabilities
        .map((capability) =>
          typeof capability === "object" && capability !== null
            ? (capability as { intent?: unknown }).intent
            : capability,
        )
        .filter(Boolean)
    : [];
  const intents = [
    record.capability,
    record.kind,
    record.intent,
    ...capabilityIntents,
  ]
    .map((value) => String(value).toLowerCase().trim())
    .filter(Boolean);
  const tabs = new Set<TryTab>();
  for (const intent of intents) {
    const tab = CAPABILITY_TABS[intent] ?? INTENT_TABS[intent];
    // Model rows lack the runnable descriptor required by Workflow. Only the
    // separately discovered workflow catalog can populate that picker.
    if (tab && tab !== "workflow") tabs.add(tab);
  }
  if (tabs.has("image") && !makesPicturesFromText(record)) tabs.delete("image");
  return [...tabs];
}

/**
 * "Make a picture" sends a prompt and nothing else, so it lists only models
 * that make a picture from a prompt. The catalog says what each model does:
 * its capability intents ("image.generate" makes; "image.edit" and
 * "image.analyze" start from a picture), its input modalities, and an
 * optional `task` for models that make a different kind of picture (texture
 * tiles). Rows without that detail keep the old behaviour.
 */
function makesPicturesFromText(record: Record<string, unknown>) {
  const task = stringValue(record.task);
  if (task && task !== "text-to-image") return false;
  const summaries = Array.isArray(record.capabilities)
    ? (record.capabilities as unknown[]).filter(
        (item): item is Record<string, unknown> =>
          typeof item === "object" && item !== null,
      )
    : [];
  const imageIntents = summaries.filter(
    (item) => String(item.intent ?? "").split(".")[0] === "image",
  );
  if (!imageIntents.length) return true;
  return imageIntents.some((item) => {
    if (item.intent !== "image.generate") return false;
    const input = Array.isArray(item.input_modalities)
      ? item.input_modalities
      : [];
    return !input.includes("image");
  });
}

function costLabel(value: unknown) {
  if (typeof value !== "object" || value === null) return "";
  const cost = value as { kind?: unknown; price?: unknown };
  if (cost.kind === "free") return "";
  if (cost.kind === "paid") {
    return cost.price == null ? "Paid" : `Paid · ${String(cost.price)}`;
  }
  return "";
}

function localityLabel(record: Record<string, unknown>) {
  if (record.locality === "local") return "Local";
  if (record.locality === "remote") {
    const provider = stringValue(record.provider);
    return provider ? `Remote · ${provider}` : "Remote";
  }
  return "";
}

function workflowExampleBody(workflow: WorkflowOption) {
  return requestIsMultipart(workflow)
    ? (workflow.example_request?.form ?? {})
    : (workflow.example_request?.body ?? {});
}

function requestIsMultipart(workflow: WorkflowOption) {
  return workflow.example_request?.content_type?.includes(
    "multipart/form-data",
  );
}

function selectedOrDefaultModel(
  current: string,
  options: CatalogOption[],
  explicit: boolean,
) {
  if (!options.length) return explicit ? current : "";
  // A model that an alias stands for is selected through the alias.
  const standIn = options.find((option) =>
    option.alsoKnownAs?.includes(current),
  );
  if (standIn) return standIn.id;
  if (explicit) return current;
  if (options.some((option) => option.id === current)) return current;
  return options.find((option) => option.recommended)?.id ?? options[0].id;
}

export default function TryPage() {
  const searchParams = useSearchParams();
  const requestedWorkflow = searchParams.get("workflow");
  const requestedCapability = searchParams.get("capability");
  const requestedModel = searchParams.get("model") ?? searchParams.get("alias");
  const requestedCapabilityKey = requestedCapability?.toLowerCase().trim();
  const requestedTab = requestedTabFor(requestedCapabilityKey);
  const requestedKind = requestedCapabilityKey
    ? (REQUESTED_KINDS[requestedCapabilityKey] ?? null)
    : null;
  // A model in the link is a deliberate choice for this task, even when the
  // catalog does not list it. A kind of work names workflows, not models.
  const linkedModelTab: TryTab | null =
    requestedModel && !requestedKind ? (requestedTab ?? "chat") : null;
  // "Show all workflows" (or another task, for a kind Try cannot run)
  // clears what the link asked for; a new link asks again.
  const [kindCleared, setKindCleared] = useState(false);
  const [kindKey, setKindKey] = useState(requestedCapabilityKey);
  if (kindKey !== requestedCapabilityKey) {
    setKindKey(requestedCapabilityKey);
    setKindCleared(false);
  }
  const kindFilter =
    requestedKind && !requestedKind.untryable && !kindCleared && !requestedWorkflow
      ? requestedKind
      : null;
  const [key, setKey] = useState<TryKey | null>(null);
  const keyGeneration = useRef(0);
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TryTab>("chat");
  const viewer = useViewer();
  const personViewer = viewer.authenticated && !viewer.serviceAdmin;
  // Prompt memory: whether this person's runs keep their words. Read once;
  // the switch below starts from it and is remembered for the session.
  const [promptMemory, setPromptMemory] = useState<
    MemberPromptMemory | null | undefined
  >(undefined);
  const [forget, setForgetState] = useState(false);
  const [model, setModel] = useState(
    () =>
      Object.fromEntries(
        Object.entries(DEFAULTS).map(([tab, value]) => [tab, value.model]),
      ) as Record<TryTab, string>,
  );
  const [prompt, setPrompt] = useState(
    () =>
      Object.fromEntries(
        Object.entries(DEFAULTS).map(([tab, value]) => [tab, value.prompt]),
      ) as Record<TryTab, string>,
  );
  const [voicesByModel, setVoicesByModel] = useState<Record<string, string>>({});
  const [decisionState, setDecisionState] = useState(
    "It is raining and the bus is 20 minutes late.",
  );
  const [options, setOptions] = useState("Take a taxi\nWait for the bus");
  const [file, setFile] = useState<File | null>(null);
  const [workflowFiles, setWorkflowFiles] = useState<
    Record<string, File | null>
  >({});
  // Run was pressed with something missing: show each field's message.
  const [workflowChecked, setWorkflowChecked] = useState(false);
  const formRefs = useRef<Partial<Record<TryTab, HTMLFormElement | null>>>({});
  const [checkingJob, setCheckingJob] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);
  const [recordingError, setRecordingError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const objectUrls = useRef<string[]>([]);
  const mounted = useRef(true);
  const acquiringMicrophone = useRef(false);
  const activeTabRef = useRef<TryTab>("chat");
  const explicitModelTabs = useRef<Set<TryTab>>(new Set());
  const initializedWorkflowId = useRef<string | null>(null);
  const chunks = useRef<Blob[]>([]);
  const runIds = useRef<Partial<Record<TryTab, number>>>({});
  const [running, setRunning] = useState<TryTab | null>(null);
  const [results, setResults] = useState<Partial<Record<TryTab, Result>>>({});
  const [errors, setErrors] = useState<Partial<Record<TryTab, TryError>>>({});
  const [catalog, setCatalog] = useState<
    Partial<Record<TryTab, CatalogOption[]>>
  >({});
  const catalogRef = useRef<CatalogGroups>({});
  const catalogRequests = useRef<
    Partial<Record<CatalogSection, AbortController>>
  >({});
  const [catalogState, setCatalogState] = useState<
    Record<CatalogSection, CatalogState>
  >({ models: "idle", workflows: "idle" });
  const [catalogPlan, setCatalogPlan] = useState<
    { id: string; name: string } | null | undefined
  >(undefined);
  const [accessRefresh, setAccessRefresh] = useState<"idle" | "loading" | "complete">("idle");
  const catalogLoading = Object.values(catalogState).some((state) => state === "loading");
  const [unknownModelCount, setUnknownModelCount] = useState(0);
  const speechChoices = catalog.speech?.find((option) => option.id === model.speech)?.speechVoices;
  const voice = selectedVoice(speechChoices, voicesByModel[model.speech]);
  const parsedOptions = useMemo(
    () =>
      options
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    [options],
  );
  const workflowChoices = useMemo(
    () =>
      (catalog.workflow ?? []).filter(
        (option) => !kindFilter || workflowMakes(option, kindFilter),
      ),
    [catalog.workflow, kindFilter],
  );
  const selectedWorkflow = useMemo(
    () =>
      workflowChoices.find((option) => option.id === model.workflow)?.workflow,
    [workflowChoices, model.workflow],
  );
  const owner = viewer.admin;
  const workflowAvailability = selectedWorkflow
    ? workflowAvailabilityView(selectedWorkflow.availability, {
        owner,
        engineBinding: selectedWorkflow.engine_binding,
      })
    : null;
  const workflowOutputKind = outputKindOf(selectedWorkflow?.outputs);
  const workflowProblems = useMemo(
    () =>
      workflowChecked && selectedWorkflow
        ? workflowInputProblems(
            prompt.workflow,
            selectedWorkflow.inputs,
            selectedWorkflow.inputMapping,
            workflowOutputKind,
            workflowFiles,
          )
        : {},
    [
      workflowChecked,
      selectedWorkflow,
      prompt.workflow,
      workflowOutputKind,
      workflowFiles,
    ],
  );
  useEffect(() => {
    mounted.current = true;
    const urls = objectUrls.current;
    return () => {
      mounted.current = false;
      keyGeneration.current += 1;
      if (recorder.current?.state === "recording") recorder.current.stop();
      stream.current?.getTracks().forEach((track) => track.stop());
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    forgetStoredTryKey();
  }, []);

  useEffect(() => {
    keyGeneration.current += 1;
    if (!viewer.authenticated) {
      setKey(null);
      setKeyBusy(false);
      setKeyError(null);
    }
  }, [viewer.authenticated]);

  useEffect(() => {
    if (requestedTab) setActiveTab(requestedTab);
  }, [requestedTab]);

  useEffect(() => {
    try {
      setForgetState(sessionStorage.getItem(FORGET_SESSION_KEY) === "1");
    } catch {
      // Storage can be blocked; the switch then starts off.
    }
  }, []);

  useEffect(() => {
    if (!personViewer) return;
    let live = true;
    const refresh = () => {
      void accountApi.promptMemory().then(
        (response) => live && setPromptMemory(response.prompt_memory),
        () => live && setPromptMemory(null),
      );
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      live = false;
      window.removeEventListener("focus", refresh);
    };
  }, [personViewer]);

  useEffect(() => {
    if (!requestedModel || !linkedModelTab) return;
    explicitModelTabs.current.add(linkedModelTab);
    setModel((current) => ({ ...current, [linkedModelTab]: requestedModel }));
  }, [linkedModelTab, requestedModel]);

  useEffect(() => {
    if (requestedWorkflow) {
      setActiveTab("workflow");
      setModel((current) => ({ ...current, workflow: requestedWorkflow }));
    }
  }, [requestedWorkflow]);

  useEffect(() => {
    void loadCatalog();
    const requests = catalogRequests.current;
    return () => {
      for (const controller of Object.values(requests)) controller.abort();
    };
    // The catalog is safe to read without a public API key. Run refreshes it
    // after minting a key so fallback model-list gateways still work.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedCapabilityKey, requestedWorkflow]);

  useEffect(() => {
    if (!selectedWorkflow) {
      initializedWorkflowId.current = null;
      return;
    }
    if (initializedWorkflowId.current === selectedWorkflow.id) return;
    initializedWorkflowId.current = selectedWorkflow.id;
    const body = workflowExampleBody(selectedWorkflow);
    const inputs = workflowDefaults(
      selectedWorkflow.inputs,
      body,
      selectedWorkflow.inputMapping,
    );
    setPrompt((current) => ({
      ...current,
      workflow: JSON.stringify(inputs, null, 2),
    }));
    setWorkflowFiles({});
    setWorkflowChecked(false);
  }, [selectedWorkflow]);

  useEffect(() => {
    activeTabRef.current = activeTab;
    if (activeTab !== "transcribe") stopRecording();
    // On a phone the strip scrolls; keep the chosen chip in view.
    document
      .querySelector<HTMLElement>(
        '[aria-label="Try capability"] [data-state="active"]',
      )
      ?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
  }, [activeTab]);

  const keepState: TryKeepState | null = personViewer
    ? promptMemory === undefined
      ? null
      : tryKeepState(promptMemory)
    : null;

  function setForget(next: boolean) {
    setForgetState(next);
    try {
      if (next) sessionStorage.setItem(FORGET_SESSION_KEY, "1");
      else sessionStorage.removeItem(FORGET_SESSION_KEY);
    } catch {
      // A blocked store only means the choice lasts for this page.
    }
  }

  function chooseTab(tab: TryTab) {
    setActiveTab(tab);
    // A kind Try cannot run was a notice about the link; choosing a task
    // answers it.
    if (requestedKind?.untryable) setKindCleared(true);
  }

  async function retryCatalog(section: CatalogSection) {
    const loaded = await loadCatalogSection(section, key?.secret);
    // Without a key only the account catalog can be read. Run is off until
    // a list loads, so preparing the key, as Run does, also tries the
    // gateway's own model list.
    if (!loaded && section === "models" && !key?.secret && mounted.current)
      await prepareKey();
  }

  async function prepareKey() {
    const generation = keyGeneration.current;
    const current = () => mounted.current && generation === keyGeneration.current;
    setKeyBusy(true);
    setKeyError(null);
    try {
      const account = await accountApi.session();
      if (!current()) return null;
      if (!account.authenticated) {
        setKey(null);
        throw new Error("Sign in to create a Try key.");
      }
      // Only reuse a key whose secret this page still holds. A reload or
      // another tab asks for a fresh key without touching other tabs' keys.
      const cached = key;
      const credential = await accountApi.tryKey(cached?.id ?? null);
      if (!current()) return null;
      const secret = credential.reused ? cached?.secret : credential.api_key;
      if (!secret || (credential.reused && cached?.id !== credential.id))
        throw new Error("Could not prepare a Try key. Try again.");
      const prepared = {
        id: credential.id,
        secret,
        appName: credential.app_name,
      };
      setKey(prepared);
      const preparedCatalog = await loadCatalog(secret);
      return current() ? { ...prepared, catalog: preparedCatalog } : null;
    } catch (error) {
      if (current()) setKeyError(
        error instanceof Error
          ? error.message
          : "Could not prepare a Try key. Try again.",
      );
      return null;
    } finally {
      if (current()) setKeyBusy(false);
    }
  }

  async function revokeTryKey() {
    if (!key) return;
    setKeyBusy(true);
    setKeyError(null);
    try {
      await accountApi.revokeKey(key.id);
      forgetStoredTryKey();
      setKey(null);
    } catch (error) {
      setKeyError(
        error instanceof Error
          ? error.message
          : "Could not revoke the Try key. Try again.",
      );
    } finally {
      setKeyBusy(false);
    }
  }

  async function startRecording() {
    // One permission request at a time: a second click while the prompt is
    // open would acquire a second stream and recorder.
    if (acquiringMicrophone.current) return;
    acquiringMicrophone.current = true;
    setRecordingError(null);
    let mediaStream: MediaStream | null = null;
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });
      if (!mounted.current || activeTabRef.current !== "transcribe") {
        mediaStream.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = mediaStream;
      const acquiredStream = mediaStream;
      const mimeType = ["audio/webm", "audio/mp4", "audio/ogg"].find(
        (type) => MediaRecorder.isTypeSupported?.(type),
      );
      const mediaRecorder = new MediaRecorder(
        mediaStream,
        mimeType ? { mimeType } : undefined,
      );
      chunks.current = [];
      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      mediaRecorder.onstop = () => {
        acquiredStream.getTracks().forEach((track) => track.stop());
        stream.current = null;
        const type = mediaRecorder.mimeType || chunks.current[0]?.type || mimeType || "";
        const extension = type.split(";")[0] === "audio/mp4"
          ? "m4a"
          : type.split(";")[0] === "audio/ogg" ? "ogg"
          : type.split(";")[0] === "audio/webm" ? "webm" : "bin";
        const blob = new Blob(chunks.current, { type });
        setFile(new File([blob], `recording.${extension}`, { type }));
        setRecording(false);
      };
      recorder.current = mediaRecorder;
      mediaRecorder.start();
      setRecording(true);
    } catch {
      mediaStream?.getTracks().forEach((track) => track.stop());
      stream.current = null;
      if (mounted.current && activeTabRef.current === "transcribe")
        setRecordingError(
          "Recording is not available in this browser. Check microphone access and try again.",
        );
    } finally {
      acquiringMicrophone.current = false;
    }
  }

  function stopRecording() {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  function nextModelForCatalog(
    grouped: CatalogGroups,
    sourceModel: Record<TryTab, string> = model,
  ) {
    const next = { ...sourceModel };
    const workflows = (grouped.workflow ?? []).filter(
      (option) => !kindFilter || workflowMakes(option, kindFilter),
    );
    // A link that names a kind of work and a model picks that workflow
    // when it is one of them; any other name is not a workflow.
    const linkedWorkflow =
      kindFilter && requestedModel
        ? workflows.find(
            (option) =>
              option.id === requestedModel ||
              option.workflow?.short_id === requestedModel ||
              option.workflow?.aliases?.includes(requestedModel),
          )
        : undefined;
    if (linkedWorkflow && !workflows.some((option) => option.id === next.workflow))
      next.workflow = linkedWorkflow.id;
    for (const tab of TAB_ITEMS) {
      const options =
        tab.value === "workflow" ? workflows : (grouped[tab.value] ?? []);
      next[tab.value] = selectedOrDefaultModel(
        next[tab.value],
        options,
        explicitModelTabs.current.has(tab.value),
      );
    }
    if (
      requestedWorkflow &&
      grouped.workflow?.some((option) => option.id === requestedWorkflow)
    ) {
      next.workflow = requestedWorkflow;
    }
    return next;
  }

  async function loadCatalog(secret?: string): Promise<CatalogGroups | null> {
    await Promise.all([
      loadCatalogSection("models", secret),
      loadCatalogSection("workflows"),
    ]);
    return mounted.current ? catalogRef.current : null;
  }

  /** True when this section's list loaded (even empty). */
  async function loadCatalogSection(
    section: CatalogSection,
    secret?: string,
  ): Promise<boolean> {
    catalogRequests.current[section]?.abort();
    const controller = new AbortController();
    catalogRequests.current[section] = controller;
    const current = () =>
      mounted.current &&
      !controller.signal.aborted &&
      catalogRequests.current[section] === controller;
    if (section === "models") setCatalogPlan(undefined);
    setCatalogState((states) => ({ ...states, [section]: "loading" }));
    try {
      const response = await fetch(
        section === "models" ? "/me/api/models/catalog" : "/me/api/workflows",
        {
          credentials: "same-origin",
          signal: controller.signal,
        },
      );
      let grouped: CatalogGroups;
      let unknownCount = 0;
      let accountPlan: typeof catalogPlan = undefined;
      if (section === "workflows") {
        if (!response.ok) throw new Error("Workflow catalog unavailable");
        grouped = { workflow: groupWorkflows(await response.json()) };
      } else if (response.ok) {
        const payload = await response.json();
        grouped = groupCatalog(payload);
        const plan = payload.access_plan;
        accountPlan = plan === null ? null
          : plan && typeof plan.id === "string" && typeof plan.name === "string"
            ? { id: plan.id, name: plan.name }
            : undefined;
      } else if (secret) {
        const fallback = await fetch("/openai/v1/models", {
          headers: { authorization: `Bearer ${secret}` },
          signal: controller.signal,
        });
        if (!fallback.ok) throw new Error("Model catalog unavailable");
        const payload: unknown = await fallback.json();
        if (!payload || typeof payload !== "object" ||
            !Array.isArray((payload as { data?: unknown }).data))
          throw new Error("Invalid fallback model catalog: expected a data array");
        const rows = (payload as { data: unknown[] }).data;
        grouped = groupCatalog({ aliases: [], models: rows });
        unknownCount = rows.filter((row) => {
          if (!row || typeof row !== "object") return false;
          const record = row as Record<string, unknown>;
          return Boolean(stringValue(record.id) && !capabilitiesFor(record).length);
        }).length;
      } else {
        throw new Error("Model catalog unavailable");
      }
      if (!current()) return false;
      const merged =
        section === "workflows"
          ? { ...catalogRef.current, workflow: grouped.workflow }
          : { ...grouped, workflow: catalogRef.current.workflow };
      catalogRef.current = merged;
      setCatalog(merged);
      if (section === "models") {
        setUnknownModelCount(unknownCount);
        setCatalogPlan(accountPlan);
      }
      setModel((selected) => nextModelForCatalog(merged, selected));
      const hasOptions = Object.values(grouped).some(
        (options) => options?.length,
      );
      setCatalogState((states) => ({
        ...states,
        [section]: hasOptions ? "ready" : "empty",
      }));
      return true;
    } catch {
      if (current())
        setCatalogState((states) => ({ ...states, [section]: "error" }));
      return false;
    }
  }

  function buildRequest(
    tab: TryTab,
    selectedModel: Record<TryTab, string> = model,
    selectedCatalog: CatalogGroups = catalog,
    requestWorkflow: WorkflowOption | undefined = selectedWorkflow,
  ): RequestInfo {
    if (tab === "chat") {
      return {
        path: "/chat/completions",
        body: {
          model: selectedModel.chat,
          messages: [{ role: "user", content: prompt.chat }],
        },
      };
    }
    if (tab === "image") {
      return {
        path: "/images/generations",
        body: {
          model: selectedModel.image,
          prompt: prompt.image,
          size: "1024x1024",
        },
      };
    }
    if (tab === "speech") {
      const voice = selectedVoice(selectedCatalog.speech?.find((option) => option.id === selectedModel.speech)?.speechVoices, voicesByModel[selectedModel.speech]);
      return {
        path: "/audio/speech",
        body: { model: selectedModel.speech, ...(voice ? { voice } : {}), input: prompt.speech },
      };
    }
    if (tab === "transcribe") {
      return {
        path: "/audio/transcriptions",
        body: { model: selectedModel.transcribe },
        multipart: true,
      };
    }
    if (tab === "decide") {
      return {
        path: "/v1/systemone",
        body: {
          model: selectedModel.decide,
          state: decisionState,
          questions: {
            route: {
              type: "choice",
              instructions: prompt.decide,
              criteria: Object.fromEntries(
                parsedOptions.map((option) => [option, option]),
              ),
            },
          },
        },
      };
    }
    if (tab === "workflow") {
      const route = requestWorkflow?.example_request?.path;
      if (!route) {
        throw new Error(
          "This workflow does not include a runnable example request yet.",
        );
      }
      const multipart = requestIsMultipart(requestWorkflow);
      const values = parseWorkflowInputs(
        prompt.workflow || "{}",
        requestWorkflow.inputs,
        requestWorkflow.inputMapping,
      );
      const body = multipart ? {} : values;
      const formBody = multipart ? values : {};
      if (multipart) {
        for (const input of requestWorkflow.inputs) {
          const mapping = requestWorkflow.inputMapping?.[input.name];
          if (
            mapping?.in !== "file+extension" ||
            !mapping.field ||
            !workflowFiles[input.name]
          )
            continue;
          const wasString = typeof formBody.extensions === "string";
          const extensions = wasString
            ? JSON.parse(formBody.extensions as string)
            : { ...((formBody.extensions as Record<string, unknown>) ?? {}) };
          extensions["com.azizam.workflow"] = {
            ...extensions["com.azizam.workflow"],
            [mapping.field]: { multipart_field: mapping.field },
          };
          formBody.extensions = wasString
            ? JSON.stringify(extensions)
            : extensions;
        }
      }

      if (
        !multipart &&
        body.model === undefined &&
        body.request === undefined
      ) {
        body.model = requestWorkflow.short_id ?? requestWorkflow.id;
      }
      return {
        path: route,
        body,
        form: formBody,
        files: workflowFileSpecs(requestWorkflow).map((file) => ({
          ...file,
          examplePath: workflowFiles[file.inputName]?.name ?? file.examplePath,
          type: workflowFiles[file.inputName]?.type || file.type,
          selected: Boolean(workflowFiles[file.inputName]),
        })),
        multipart,
      };
    }
    return {
      path: "/embeddings",
      body: { model: selectedModel.embed, input: prompt.embed },
    };
  }

  async function run(tab: TryTab, event: FormEvent) {
    event.preventDefault();
    if (tab === "chat" && !prompt.chat.trim()) {
      setErrors((current) => ({
        ...current,
        chat: { message: "Enter a prompt before running chat." },
      }));
      return;
    }
    // A sign-out or unmount (both advance keyGeneration) ends the identity
    // this run started under.
    const runGeneration = keyGeneration.current;
    const runId = (runIds.current[tab] ?? 0) + 1;
    runIds.current[tab] = runId;
    const superseded = () => !mounted.current ||
      runGeneration !== keyGeneration.current || runIds.current[tab] !== runId;
    if (tab === "workflow" && workflowAvailability?.runnable === false) return;
    if (tab === "workflow" && selectedWorkflow?.example_request?.path) {
      // Nothing doomed goes to the gateway: say what is missing, on the field.
      const problems = workflowInputProblems(
        prompt.workflow,
        selectedWorkflow.inputs,
        selectedWorkflow.inputMapping,
        workflowOutputKind,
        workflowFiles,
      );
      const first = selectedWorkflow.inputs.find(
        (input) => problems[input.name],
      );
      setWorkflowChecked(true);
      if (first) {
        setErrors((current) => ({
          ...current,
          workflow: isFileInput(first, selectedWorkflow.inputMapping)
            ? { message: "Choose every picture this workflow needs first." }
            : undefined,
        }));
        window.requestAnimationFrame(() =>
          document.getElementById(`workflow-input-${first.name}`)?.focus(),
        );
        return;
      }
    }
    let runKey = key;
    let preparedCatalog: CatalogGroups | null = null;
    if (!runKey?.secret) {
      const prepared = await prepareKey();
      runKey = prepared;
      preparedCatalog = prepared?.catalog ?? null;
      if (!runKey?.secret) return;
    }
    if (superseded()) return;
    const runCatalog = preparedCatalog ?? catalog;
    const runWorkflow = runCatalog.workflow?.find(
      (option) => option.id === model.workflow,
    )?.workflow;
    if (tab === "workflow" && (!runWorkflow ||
      !workflowAvailabilityView(runWorkflow.availability, {
        owner, engineBinding: runWorkflow.engine_binding,
      }).runnable || !runWorkflow.example_request?.path)) {
      setErrors((current) => ({
        ...current,
        workflow: { message: runWorkflow
          ? workflowAvailabilityView(runWorkflow.availability, {
              owner, engineBinding: runWorkflow.engine_binding,
            }).text || "This workflow does not include a runnable example request yet."
          : "This workflow is no longer available." },
      }));
      return;
    }
    const selectedModel = preparedCatalog
      ? nextModelForCatalog(preparedCatalog, model)
      : model;
    if (!selectedModel[tab]) {
      setErrors((current) => ({
        ...current,
        [tab]: { message: "Choose a model with a known capability before running it." },
      }));
      return;
    }
    setRunning(tab);
    if (tab === "workflow") setJobError(null);
    setErrors((current) => ({ ...current, [tab]: undefined }));
    try {
      const built = buildRequest(tab, selectedModel, runCatalog, runWorkflow);
      // Preserve the promise shown when Run was pressed, even if a refresh
      // finds that another tab has since enabled memory.
      let refreshedState = keepState;
      if (personViewer && MEMORY_TABS.has(tab)) {
        try {
          const latest = await accountApi.promptMemory();
          setPromptMemory(latest.prompt_memory);
          refreshedState = tryKeepState(latest.prompt_memory);
        } catch {
          setPromptMemory(null);
          refreshedState = "unknown";
        }
        if (superseded()) return;
      }
      if (
        personViewer &&
        MEMORY_TABS.has(tab) &&
        (refreshedState === "unknown" ||
          ((keepState === "member-off" || keepState === "household-off") &&
            refreshedState === "choice"))
      ) {
        // Keep the current run's privacy promise visible after a refresh.
        setForget(true);
      }
      const forgetRun =
        MEMORY_TABS.has(tab) &&
        (forget ||
          keepState === "member-off" ||
          keepState === "household-off" ||
          refreshedState === "member-off" ||
          refreshedState === "household-off" ||
          (personViewer && refreshedState === "unknown"));
      const request: RequestInfo = forgetRun
        ? built.multipart
          ? { ...built, form: withForget(built.form ?? {}) }
          : {
              ...built,
              body: withForget((built.body ?? {}) as Record<string, unknown>),
            }
        : built;
      const promptNotSaved = forgetRun;
      let result: Awaited<ReturnType<typeof publicRequest>> & {
        requestId?: string;
      };
      if (tab === "image") {
        const finished = await runImageAsJob(request, runKey.secret);
        if (!finished) return;
        result = finished;
      } else if (request.multipart) {
        const form = new FormData();
        if (tab === "workflow") {
          for (const fileSpec of request.files ?? []) {
            const field = fileSpec.field;
            const selectedFile = workflowFiles[fileSpec.inputName];
            if (!selectedFile && fileSpec.required !== false) {
              throw new Error("Choose every picture this workflow needs first.");
            }
            if (selectedFile) {
              form.append(field, selectedFile);
            }
          }
          const fileFields = new Set(
            (request.files ?? []).map((file) => file.field),
          );
          for (const [name, value] of Object.entries(request.form ?? {})) {
            if (fileFields.has(name) || value === undefined || value === null)
              continue;
            if (typeof value === "object") {
              form.set(name, JSON.stringify(value));
            } else {
              form.set(name, String(value));
            }
          }
        } else {
          if (!file) {
            throw new Error("Choose an audio file or record one first.");
          }
          form.set("model", selectedModel.transcribe);
          form.set("file", file);
        }
        result = await publicRequest(request.path, runKey.secret, {
          method: "POST",
          body: form,
        });
      } else {
        result = await publicRequest(request.path, runKey.secret, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(request.body),
        });
      }
      if (superseded()) return;
      const lookup = receiptLookup(result.response, result.payload, result.requestId);
      const receipt = tab === "chat"
        ? { receiptId: null, receiptLookup: lookup }
        : await checkReceipt(lookup);
      if (superseded()) return;
      const oldUrls = [
        results[tab]?.audioUrl,
        ...(results[tab]?.media?.map((item) => item.url) ?? []),
      ].filter((url): url is string => Boolean(url?.startsWith("blob:")));
      oldUrls.forEach((url) => URL.revokeObjectURL(url));
      objectUrls.current = objectUrls.current.filter(
        (url) => !oldUrls.includes(url),
      );
      setResults((current) => ({
        ...current,
        [tab]: {
          ...formatResult(
            tab,
            result.payload,
            result.elapsedMs,
            costFrom(
              result.payload,
              result.response,
              preparedCatalog?.[tab]?.find(
                (option) => option.id === selectedModel[tab],
              )?.locality ??
                catalog[tab]?.find((option) => option.id === selectedModel[tab])
                  ?.locality,
            ),
            receipt.receiptId,
            request,
          ),
          receiptLookup: receipt.receiptLookup,
          imageModel: tab === "image"
            ? runCatalog.image?.find((option) => option.id === selectedModel.image)?.label ?? selectedModel.image
            : undefined,
          checkingReceipt: tab === "chat",
          runId,
          promptNotSaved,
        },
      }));
      if (tab === "chat") {
        void timedReceipt(lookup).then((checked) => {
          if (superseded()) return;
          setResults((current) => current[tab]?.runId === runId
            ? { ...current, [tab]: { ...current[tab]!, ...checked,
                receiptLookup: checked.receiptLookup, checkingReceipt: false } }
            : current);
        });
      }
    } catch (error) {
      if (superseded()) return;
      const lost =
        error instanceof LostRequestError || isNetworkFailure(error)
          ? lostCopy(tab)
          : null;
      setErrors((current) => ({
        ...current,
        [tab]: {
          ...(lost ? { ...lost, lost: true } : {}),
          message:
            lost?.message ??
            (tab === "image" && error instanceof PublicRequestError
              ? error.status === 403 && error.message !== UNKNOWN_FAILURE
                ? error.message
                : "We couldn’t make this picture. Try again, or choose another model."
              :
            (error instanceof ApiError &&
            tab === "decide" &&
            error.status === 404
              ? "Decisions not enabled on this gateway"
              : error instanceof Error
                ? error.message
                : "This request could not be completed.")),
          ...(error instanceof PublicRequestError
            ? {
                layer: error.layer,
                receiptId: receiptFrom(error.response, error.payload),
                technical: [
                  `HTTP ${error.status} · ${error.elapsedMs} ms`,
                  error.payload instanceof Blob
                    ? `${error.payload.type || "file"} · ${error.payload.size} bytes`
                    : JSON.stringify(error.payload, null, 2),
                ].join("\n"),
              }
            : {}),
        },
      }));
    } finally {
      setRunning(null);
    }
  }

  /**
   * A picture can outlast the page: the phone browser drops a long fetch when
   * the person switches apps. So it runs as a media job that is polled, and
   * polling resumes the moment the page is shown again. Returns null when
   * the page went away.
   */
  async function runImageAsJob(request: RequestInfo, secret: string) {
    const idempotencyKey = newIdempotencyKey();
    const startedAt = Date.now();
    const clock = performance.now();
    const wake: { fn: (() => void) | null } = { fn: null };
    const onShown = () => {
      if (document.visibilityState !== "hidden") wake.fn?.();
    };
    document.addEventListener("visibilitychange", onShown);
    window.addEventListener("online", onShown);
    window.addEventListener("pageshow", onShown);
    try {
      const outcome = await runImageJob({
        submit: async () => {
          const response = await publicRequest(IMAGE_JOB_PATH, secret, {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "idempotency-key": idempotencyKey,
            },
            body: JSON.stringify(request.body),
          });
          const job = workflowJob(response.payload);
          if (!job) {
            throw new Error("The gateway did not start a picture job.");
          }
          return { id: job.id, status: job.status, requestId: job.requestId };
        },
        poll: async (id) => {
          const response = await publicRequest(
            `/gateway/v1/media/jobs/${encodeURIComponent(id)}`,
            secret,
            { method: "GET" },
          );
          const job = workflowJob(response.payload);
          if (!job) throw new Error("The gateway did not return a job status.");
          return {
            status: job.status,
            error: job.error,
            requestId: job.requestId,
          };
        },
        content: (id) =>
          publicRequest(
            `/gateway/v1/media/jobs/${encodeURIComponent(id)}/content`,
            secret,
            { method: "GET" },
          ),
        wait: (ms) =>
          new Promise<void>((resolve) => {
            const finish = () => {
              window.clearTimeout(timer);
              wake.fn = null;
              resolve();
            };
            const timer = window.setTimeout(finish, ms);
            wake.fn = finish;
          }),
        isNetworkFailure,
        now: () => Date.now(),
        stopped: () => !mounted.current,
      });
      if (outcome.kind === "stopped") return null;
      if (outcome.kind === "failed") throw new Error(outcome.message);
      if (outcome.kind === "lost") {
        const assetId = await recentCreation(
          (request.body as { prompt?: unknown }).prompt,
          startedAt,
          outcome.jobId,
        );
        if (!assetId) throw new LostRequestError("image", outcome.jobId);
        return {
          response: new Response(null),
          payload: {
            data: [
              {
                url: `/me/api/media/${encodeURIComponent(assetId)}/download?scope=me`,
              },
            ],
          } as unknown,
          elapsedMs: Math.round(performance.now() - clock),
        };
      }
      return {
        ...outcome.content,
        elapsedMs: Math.round(performance.now() - clock),
        requestId: outcome.requestId,
      };
    } finally {
      document.removeEventListener("visibilitychange", onShown);
      window.removeEventListener("online", onShown);
      window.removeEventListener("pageshow", onShown);
    }
  }

  /** Checks a result's receipt again after the check itself failed. */
  async function recheckReceipt(tab: TryTab) {
    const lookup = results[tab]?.receiptLookup;
    if (!lookup) return;
    const update = (patch: Partial<Result>) =>
      setResults((current) => {
        const result = current[tab];
        return result?.receiptLookup === lookup
          ? { ...current, [tab]: { ...result, ...patch } }
          : current;
      });
    update({ checkingReceipt: true });
    const receipt = await timedReceipt(lookup);
    if (!mounted.current) return;
    update({
      checkingReceipt: false,
      receiptId: receipt.receiptId,
      ...(receipt.receiptLookup ? {} : { receiptLookup: undefined }),
    });
  }

  function editFailedRun(tab: TryTab) {
    const form = formRefs.current[tab];
    if (!form) return;
    form.scrollIntoView?.({ block: "start", behavior: "smooth" });
    const target =
      form.querySelector<HTMLElement>('[aria-invalid="true"]') ??
      form.querySelector<HTMLElement>(
        "textarea:not([disabled]), input:not([type=file]):not([type=hidden]):not([disabled])",
      ) ??
      form.querySelector<HTMLElement>('button[type="submit"]');
    target?.focus({ preventScroll: true });
  }

  async function checkWorkflowJob() {
    const previous = results.workflow;
    if (!previous?.job || !key?.secret) return;
    setCheckingJob(true);
    setJobError(null);
    try {
      const status = await publicRequest(previous.job.pollPath, key.secret, {
        method: "GET",
      });
      const job = workflowJob(status.payload, {
        poll: { path: previous.job.pollPath },
        content: { path: previous.job.contentPath },
      });
      if (!job)
        throw new Error(
          "The gateway did not return a job status. Check again.",
        );
      let payload = status.payload;
      if (["succeeded", "completed"].includes(job.status)) {
        const content = await publicRequest(job.contentPath, key.secret, {
          method: "GET",
        });
        payload = content.payload;
      }
      const requestId =
        previous.receiptId ?? job.requestId ?? previous.job.requestId;
      const receipt = await checkReceipt({
        requestId: requestId ?? null,
        assetIds: [],
      });
      setResults((current) => ({
        ...current,
        workflow: {
          ...previous,
          job,
          media: workflowOutputMedia(payload),
          technical:
            payload instanceof Blob
              ? `${payload.type ? `${payload.type} · ` : ""}${payload.size} bytes`
              : JSON.stringify(payload, null, 2),
          receiptId: receipt.receiptId,
          receiptLookup: receipt.receiptLookup,
        },
      }));
    } catch (error) {
      setJobError(
        error instanceof Error
          ? error.message
          : "Could not check the job. Try again.",
      );
    } finally {
      setCheckingJob(false);
    }
  }

  function workflowOutputMedia(payload: unknown): WorkflowMedia[] {
    if (payload instanceof Blob) {
      const url = URL.createObjectURL(payload);
      objectUrls.current.push(url);
      const kind = payload.type.startsWith("image/")
        ? "image"
        : payload.type.startsWith("audio/")
          ? "audio"
          : payload.type.startsWith("video/")
            ? "video"
            : "file";
      return [{ kind, url, label: "Workflow output", mediaType: payload.type }];
    }
    const kind =
      selectedWorkflow?.outputs?.[0]?.kind ??
      selectedWorkflow?.outputs?.[0]?.type;
    return mediaFrom(payload, kind);
  }

  function formatResult(
    tab: TryTab,
    payload: unknown,
    elapsedMs: number,
    cost: string | undefined,
    receiptId: string | null,
    request: RequestInfo,
  ): Result {
    const common = { elapsedMs, cost, receiptId, request };
    const raw =
      payload instanceof Blob
        ? `${payload.type ? `${payload.type} · ` : ""}${payload.size} bytes`
        : JSON.stringify(payload, null, 2);
    // A success with nothing in it says what is missing; the raw answer
    // stays under Technical details (PF-007).
    const nothing = (title: string, missing: string): Result => ({
      title,
      body: "",
      missing,
      technical: raw,
      ...common,
    });
    if (tab === "chat") {
      const text = textFromChat(payload);
      return text
        ? { title: "Chat result", body: text, copyAnswer: true,
            answerNote: (() => {
              const reason = (payload as { choices?: Array<{ finish_reason?: string }> })?.choices?.[0]?.finish_reason;
              return reason === "length"
                ? "This answer stopped early because it reached its length limit."
                : reason === "content_filter"
                  ? "This answer was cut short by a content filter." : undefined;
            })(), ...common }
        : nothing("Chat result", "reply");
    }
    if (tab === "image") {
      const imageUrl = imageFrom(payload);
      return imageUrl
        ? { title: "Image result", body: "Image generated.", imageUrl, ...common }
        : nothing("Image result", "picture");
    }
    if (tab === "speech") {
      if (!(payload instanceof Blob) || !payload.size)
        return nothing("Speech result", "audio");
      const audioUrl = URL.createObjectURL(payload);
      objectUrls.current.push(audioUrl);
      return { title: "Speech result", body: "Audio generated.", audioUrl, ...common };
    }
    if (tab === "transcribe") {
      const text = (payload as { text?: unknown } | null)?.text;
      return typeof text === "string" && text.trim()
        ? { title: "Transcription result", body: text, copyAnswer: true, ...common }
        : nothing("Transcription result", "transcript");
    }
    if (tab === "decide") {
      return {
        title: "Decision result",
        body: "",
        probabilities: decisionRows(payload, parsedOptions),
        technical: raw,
        ...common,
      };
    }
    if (tab === "workflow") {
      const media = workflowOutputMedia(payload);
      const job =
        workflowJob(payload, selectedWorkflow?.jobRoutes) ?? undefined;
      return {
        title: "Workflow result",
        body: job
          ? `${jobPhase(job.status)}.`
          : media.length
            ? `${media.length} output${media.length === 1 ? "" : "s"} returned.`
            : "Workflow returned a response. Open Technical details to inspect it.",
        media,
        job,
        technical: raw,
        ...common,
      };
    }
    const vector = embeddingStats(payload);
    return vector
      ? { title: "Embedding result", body: "Vector created.", vector, ...common }
      : nothing("Embedding result", "result");
  }

  return (
    <div className="space-y-8">
      <PageHeader title="Try" />

      <section className="text-muted-foreground text-sm">
        {personViewer && (
          <div className="mb-3 space-y-1" role="group" aria-label="Try access">
            <p>Signed in as {viewer.name}.</p>
            <p role="status">
              {catalogPlan ? `Your access plan: ${catalogPlan.name}.`
                : ["idle", "loading"].includes(catalogState.models)
                  ? "Checking your access plan…"
                  : catalogPlan === null
                    ? "No access plan is assigned to your account. Ask the owner."
                    : "Your access plan could not be confirmed."}
              {accessRefresh === "complete" && " Access refresh complete."}
            </p>
            {viewer.admin && (
              <p>
                Checking another plan does not change your access here.{" "}
                <Link prefetch={false} href="/people/?tab=plans" className="text-foreground underline underline-offset-4">
                  Check another plan
                </Link>
                .
              </p>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-disabled={catalogLoading || accessRefresh === "loading"}
              aria-busy={catalogLoading || accessRefresh === "loading"}
              onClick={async () => {
                if (catalogLoading || accessRefresh === "loading") return;
                setAccessRefresh("loading");
                await loadCatalog(key?.secret);
                if (mounted.current) setAccessRefresh("complete");
              }}
            >
              Refresh access
            </Button>
          </div>
        )}
        {keyError && (
          <div className="mb-3">
            <ErrorState
              message={keyError}
              onRetry={() => void (key ? revokeTryKey() : prepareKey())}
            />
          </div>
        )}
        {!key ? (
          <div className="flex flex-wrap items-center gap-3">
            {keyBusy && (
              <span
                aria-busy="true"
                role="status"
                className="inline-flex items-center gap-2"
              >
                <Loader2 aria-hidden="true" className="size-4 animate-spin" />
                Getting Try ready…
              </span>
            )}
          </div>
        ) : (
          // Try manages its key; how it works is a detail (PF-011). The
          // gateway ends the key with the sign-in session
          // (ensure_try_session_key), so "ends when you sign out" holds.
          <details>
            <summary className="inline-flex min-h-11 cursor-pointer items-center">
              Key details
            </summary>
            <p>
              Try runs with its own key, which ends when you sign out.{" "}
              <button
                type="button"
                className="text-foreground min-h-11 underline underline-offset-4 disabled:opacity-50"
                onClick={() => void revokeTryKey()}
                disabled={keyBusy}
              >
                Remove this key
              </button>
            </p>
          </details>
        )}
      </section>

      {requestedKind?.untryable && !kindCleared ? (
        <p role="status" className="text-muted-foreground text-sm">
          Try can’t run {requestedKind.noun}. Choose another task.
        </p>
      ) : null}

      <Tabs value={activeTab} onValueChange={(value) => chooseTab(value as TryTab)}>
        {/* A phone has no room for seven task names in a row, and a strip
            that scrolls hides the ones past the edge. A select lists them
            all (P4); wider screens keep the tabs. */}
        <label className="block space-y-2 sm:hidden">
          <span className="text-sm font-medium">Task</span>
          <select
            className="border-input bg-card rounded-control min-h-11 w-full border px-3 text-base"
            value={activeTab}
            onChange={(event) => chooseTab(event.target.value as TryTab)}
          >
            {TAB_ITEMS.map((tab) => (
              <option key={tab.value} value={tab.value}>
                {tab.label}
              </option>
            ))}
          </select>
        </label>
        <div
          className="overflow-x-auto pb-1 max-sm:hidden"
          tabIndex={0}
          aria-label="Try capability tabs"
        >
          <TabsList aria-label="Try capability" className="min-w-max">
            {TAB_ITEMS.map((tab) => (
              <TabsTrigger
                key={tab.value}
                value={tab.value}
                // A chip keeps its full label; the strip scrolls instead of
                // squeezing chips into each other.
                className="flex-none shrink-0"
              >
                <tab.icon aria-hidden="true" />
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {TAB_ITEMS.map((tab) => {
          const currentResult = results[tab.value];
          const currentError = errors[tab.value];
          const section: CatalogSection =
            tab.value === "workflow" ? "workflows" : "models";
          const choices =
            tab.value === "workflow"
              ? workflowChoices
              : (catalog[tab.value] ?? []);
          const unavailableMessage = choices.find((option) => option.id === model[tab.value])?.unavailableMessage;
          const linked = linkedModelTab === tab.value && Boolean(model[tab.value]);
          const listed =
            catalogState[section] === "ready" ||
            catalogState[section] === "empty";
          // Run waits for the list, and needs something on it to run.
          const runBlocked = !listed || (!choices.length && !linked);
          return (
            <TabsContent key={tab.value} value={tab.value} className="mt-4">
              <div className="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <form
                  noValidate
                  ref={(node) => {
                    formRefs.current[tab.value] = node;
                  }}
                  onSubmit={(event) => void run(tab.value, event)}
                  className="bg-card min-w-0 space-y-4 rounded-xl border p-5"
                >
                  {/* The selected tab is the heading; repeating it above the
                      form said the same word twice. */}
                  {tab.help && (
                    <p className="text-muted-foreground text-sm">{tab.help}</p>
                  )}
                  <label className="block space-y-2">
                    <span className="text-sm font-medium">Model name</span>
                    <ModelSelect
                      loading={["idle", "loading"].includes(catalogState[section])}
                      value={model[tab.value]}
                      options={choices}
                      onChange={(value) => {
                        setModel((current) => ({
                          ...current,
                          [tab.value]: value,
                        }));
                      }}
                    />
                  </label>
                  {unavailableMessage ? (
                    <ServiceNotice message={unavailableMessage} />
                  ) : null}
                  {tab.value === activeTab && (
                    <CatalogNotice
                      state={catalogState[section]}
                      unknownModelCount={unknownModelCount}
                      section={section}
                      empty={
                        listed &&
                        !choices.length &&
                        !linked &&
                        !(tab.value === "workflow" && kindFilter)
                          ? EMPTY_NOUN[tab.value]
                          : null
                      }
                      owner={owner}
                      onRetry={() => void retryCatalog(section)}
                    />
                  )}
                  {tab.value === "workflow" && kindFilter && listed ? (
                    <KindNotice
                      kind={kindFilter}
                      matches={workflowChoices.length}
                      others={(catalog.workflow?.length ?? 0) > workflowChoices.length}
                      owner={owner}
                      onShowAll={() => setKindCleared(true)}
                    />
                  ) : null}
                  {tab.value === "image" && (
                    <p className="text-muted-foreground text-xs">
                      Editing, upscaling and other tools that start from a
                      picture are under{" "}
                      <button
                        type="button"
                        className="text-foreground underline underline-offset-4"
                        onClick={() => setActiveTab("workflow")}
                      >
                        Workflow
                      </button>
                      .
                    </p>
                  )}
                  {tab.value === "decide" && (
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">
                        What is happening?
                      </span>
                      <Textarea
                        value={decisionState}
                        onChange={(event) =>
                          setDecisionState(event.target.value)
                        }
                      />
                    </label>
                  )}
                  {tab.value === "workflow" && selectedWorkflow && (
                    <WorkflowInputs
                      key={selectedWorkflow.id}
                      inputs={selectedWorkflow.inputs}
                      mapping={selectedWorkflow.inputMapping}
                      outputKind={workflowOutputKind}
                      problems={workflowProblems}
                      json={prompt.workflow}
                      onChange={(json) =>
                        setPrompt((current) => ({ ...current, workflow: json }))
                      }
                      files={workflowFiles}
                      onFileChange={(inputName, nextFile) =>
                        setWorkflowFiles((current) => ({
                          ...current,
                          [inputName]: nextFile,
                        }))
                      }
                      technical={owner}
                    />
                  )}
                  {/* The request body is a builder's tool: owners only, folded away. */}
                  {tab.value === "workflow" && owner && (
                    <details className="border-t pt-3">
                      <summary className="min-h-11 cursor-pointer text-sm font-medium">
                        Advanced JSON
                      </summary>
                      <label className="block space-y-2">
                        <span className="text-sm font-medium">Inputs JSON</span>
                        <Textarea
                          value={prompt.workflow}
                          onChange={(event) =>
                            setPrompt((current) => ({
                              ...current,
                              workflow: event.target.value,
                            }))
                          }
                          className="min-h-40 font-mono"
                        />
                        <span className="text-muted-foreground text-xs">
                          This is the native request body. Extra fields are
                          preserved. File inputs are attached separately.
                        </span>
                      </label>
                    </details>
                  )}
                  {tab.value !== "transcribe" && tab.value !== "workflow" && (
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">
                        {tab.value === "decide" ? "Question" : "Prompt"}
                      </span>
                      <Textarea
                        value={prompt[tab.value]}
                        onChange={(event) =>
                          setPrompt((current) => ({
                            ...current,
                            [tab.value]: event.target.value,
                          }))
                        }
                      />
                    </label>
                  )}
                  {tab.value === "speech" && (
                    <VoicePicker
                      choices={speechChoices}
                      value={voice}
                      loading={["idle", "loading"].includes(catalogState.models)}
                      onChange={(next) => setVoicesByModel((current) => ({ ...current, [model.speech]: next }))}
                    />
                  )}
                  {tab.value === "transcribe" && (
                    <div className="space-y-3">
                      {/* ErrorState retry buttons must not submit this form. */}
                      {recordingError && (
                        <div onClick={(event) => event.preventDefault()}>
                          <ErrorState
                            heading="Could not record audio"
                            message={recordingError}
                            onRetry={() => void startRecording()}
                          />
                        </div>
                      )}
                      <label className="block space-y-2">
                        <span className="text-sm font-medium">Audio file</span>
                        <Input
                          type="file"
                          accept="audio/*"
                          onChange={(event) =>
                            setFile(event.target.files?.[0] ?? null)
                          }
                        />
                      </label>
                      <div className="flex flex-wrap items-center gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          onClick={() => void startRecording()}
                          disabled={recording}
                        >
                          <Mic aria-hidden="true" />
                          Record
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          onClick={stopRecording}
                          disabled={!recording}
                        >
                          <Pause aria-hidden="true" />
                          Stop
                        </Button>
                        {file && (
                          <span className="text-muted-foreground text-sm">
                            {file.name}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                  {tab.value === "decide" && (
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">Options</span>
                      <Textarea
                        value={options}
                        onChange={(event) => setOptions(event.target.value)}
                        aria-describedby="options-help"
                      />
                      <span
                        id="options-help"
                        className="text-muted-foreground text-xs"
                      >
                        One option per line. All listed options are sent.
                      </span>
                    </label>
                  )}
                  {MEMORY_TABS.has(tab.value) && keepState ? (
                    <PromptKeepControl
                      id={`forget-${tab.value}`}
                      state={keepState}
                      forget={forget}
                      onForget={setForget}
                      owner={owner}
                      made={
                        tab.value === "image"
                          ? "picture"
                          : tab.value === "speech"
                            ? "audio"
                            : workflowOutputKind === "image"
                              ? "picture"
                              : workflowOutputKind === "other"
                                ? "result"
                                : workflowOutputKind
                      }
                    />
                  ) : null}
                  {MEMORY_TABS.has(tab.value) &&
                  personViewer &&
                  tab.value !== "speech" ? (
                    <p className="text-muted-foreground text-meta">
                      Jobs whose prompts aren’t kept cannot resume after a
                      gateway restart.
                    </p>
                  ) : null}
                  {/* Run is the page's one primary action (D1.5b). */}
                  <Button
                    type="submit"
                    disabled={
                      running !== null ||
                      keyBusy ||
                      checkingJob ||
                      runBlocked ||
                      (tab.value === "workflow" &&
                        (!workflowAvailability ||
                          !workflowAvailability.runnable))
                    }
                  >
                    {running === tab.value ? (
                      <Loader2 aria-hidden="true" className="animate-spin" />
                    ) : (
                      <Play aria-hidden="true" />
                    )}
                    Run
                  </Button>
                  {tab.value === "workflow" && workflowAvailability?.text && (
                    <p
                      className="text-muted-foreground text-sm"
                      data-testid="workflow-availability-note"
                    >
                      {workflowAvailability.text}
                      {workflowAvailability.fix && (
                        <>
                          {" "}
                          <Link prefetch={false}
                            className="underline underline-offset-4"
                            href={workflowAvailability.fix.href}
                          >
                            {workflowAvailability.fix.action}
                          </Link>
                        </>
                      )}
                    </p>
                  )}
                </form>
                <section
                  className="bg-card min-w-0 rounded-xl border p-5"
                  aria-live="polite"
                >
                  {running === tab.value ? (
                    <div aria-busy="true" className="space-y-3">
                      {tab.value === "image" && (
                        <p
                          role="status"
                          className="text-muted-foreground text-sm"
                        >
                          Making your picture. You can switch apps; it keeps
                          going.
                        </p>
                      )}
                      <Skeleton className="h-6 w-40" />
                      <Skeleton className="h-28 w-full" />
                      <Skeleton className="h-20 w-full" />
                    </div>
                  ) : currentError ? (
                    <div className="space-y-5">
                      {/* A failed run says what failed (it was titled
                          "Couldn't load this"; D1.5b). It never repeats a
                          generation by itself: it takes the person back to the
                          form with their values kept. A catalog that failed
                          to load has its own retry button above the form. */}
                      <ErrorState
                        heading={
                          currentError.heading ??
                          (currentError.layer
                            ? LAYER_HEADINGS[currentError.layer]
                            : `${tab.label} request failed`)
                        }
                        message={currentError.message}
                        severity={currentError.lost ? "attention" : "critical"}
                        action={
                          currentError.library
                            ? { label: "Open Library", href: "/library/" }
                            : {
                                label: "Edit and run again",
                                onClick: () => editFailedRun(tab.value),
                              }
                        }
                      />
                      {currentError.receiptId && (
                        <ReceiptLink id={currentError.receiptId} />
                      )}
                      {currentError.technical && (
                        <TechnicalDetails text={currentError.technical} />
                      )}
                    </div>
                  ) : currentResult ? (
                    <ResultView
                      result={currentResult}
                      onCheckReceipt={() => void recheckReceipt(tab.value)}
                      onCheckJob={() => void checkWorkflowJob()}
                      checkingJob={checkingJob}
                      jobError={jobError}
                    />
                  ) : (
                    <EmptyState
                      icon={tab.icon}
                      title="Your result appears here"
                    />
                  )}
                </section>
              </div>
            </TabsContent>
          );
        })}
      </Tabs>
    </div>
  );
}

function ResultView({
  result,
  onCheckReceipt,
  onCheckJob,
  checkingJob,
  jobError,
}: {
  result: Result;
  onCheckReceipt: () => void;
  onCheckJob: () => void;
  checkingJob: boolean;
  jobError: string | null;
}) {
  const { copied, failed, copy } = useCopied(JSON.stringify([result.runId, result.body]));
  const probabilityRows = result.probabilities?.filter(
    (row): row is { option: string; probability: number } =>
      row.probability !== null,
  );
  const job = result.job;
  const jobStatus = job?.status.toLowerCase() ?? "";
  const noMedia = !result.media?.length;
  return (
    <div className="min-w-0 space-y-5">
      <div className="space-y-2">
        <h2 className="font-semibold">{result.title}</h2>
        {result.promptNotSaved ? (
          <p className="text-muted-foreground text-meta flex items-center gap-1.5">
            <EyeOff aria-hidden="true" className="size-4 shrink-0" />
            Prompt not kept
          </p>
        ) : null}
        {result.missing && (
          <p className="text-sm">
            The gateway returned no {result.missing}.
            {result.receiptId ? " Open the receipt for details." : ""}
          </p>
        )}
        {Boolean(result.media?.length) && (
          <p className="text-muted-foreground text-sm">
            {result.media?.length} output{result.media?.length === 1 ? "" : "s"}{" "}
            returned.
          </p>
        )}
        {result.imageUrl && (
          <div className="max-w-2xl space-y-2">
            <ResultMedia key={result.imageUrl} kind="image" url={result.imageUrl} />
            <Button asChild variant="outline">
              <a href={result.imageUrl} download="generated-picture" target="_blank" rel="noopener noreferrer">
                Download picture
              </a>
            </Button>
          </div>
        )}
        {result.audioUrl && (
          <ResultMedia kind="audio" url={result.audioUrl} label="Generated audio" />
        )}
        {result.downloadUrl && (
          <a
            href={result.downloadUrl}
            download={result.downloadName}
            className="inline-flex min-h-11 items-center underline underline-offset-4"
          >
            Download result
          </a>
        )}
        {result.vector && (
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Fact label="Dimensions" value={String(result.vector.dimensions)} />
            <Fact label="Min" value={result.vector.min.toFixed(3)} />
            <Fact label="Max" value={result.vector.max.toFixed(3)} />
            <Fact label="Mean" value={result.vector.mean.toFixed(3)} />
          </dl>
        )}
        {result.probabilities && !probabilityRows?.length ? (
          <p className="text-sm">No answer was returned.</p>
        ) : null}
        {probabilityRows?.length ? (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left">
                  <th className="p-3 font-medium">Option</th>
                  <th className="p-3 font-medium">Probability</th>
                </tr>
              </thead>
              <tbody>
                {probabilityRows.map((row) => (
                  <tr key={row.option} className="border-b last:border-0">
                    <td className="p-3">{row.option}</td>
                    <td className="p-3">
                      {`${Math.round(row.probability * 100)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {result.media?.map((item, index) => (
          <div key={`${item.url}-${index}`} className="space-y-2">
            {item.kind !== "file" && (
              <ResultMedia kind={item.kind} url={item.url} label={item.label} />
            )}
            <a
              href={item.url}
              download
              className="inline-flex min-h-11 items-center text-sm underline underline-offset-4"
            >
              Download {item.kind === "file" ? "output" : item.kind}
              {(result.media?.length ?? 0) > 1 ? ` ${index + 1}` : ""}
            </a>
          </div>
        ))}
        {job && (
          <div className="space-y-2">
            <p className="text-sm">{jobPhase(job.status)}</p>
            {jobError && <ErrorState message={jobError} onRetry={onCheckJob} />}
            {job.error && (
              <p className="text-destructive text-sm">{job.error}</p>
            )}
            {noMedia && FINISHED_JOB.includes(jobStatus) ? (
              <p className="text-sm">
                The workflow finished but returned no creation.
                {result.receiptId ? " Open its receipt." : ""}
              </p>
            ) : null}
            {noMedia && !ENDED_JOB.includes(jobStatus) && (
              <>
                <p className="text-muted-foreground text-sm">
                  The gateway is still making this. Check again to load it.
                </p>
                <Button
                  type="button"
                  variant="outline"
                  disabled={checkingJob}
                  onClick={onCheckJob}
                >
                  {checkingJob ? "Checking…" : "Check again"}
                </Button>
              </>
            )}
          </div>
        )}
        {!result.imageUrl &&
          !result.audioUrl &&
          !result.media?.length &&
          !job &&
          !result.vector &&
          !result.probabilities &&
          !result.missing && (
            <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-sm whitespace-pre-wrap">
              {result.body}
            </pre>
          )}
      </div>
      {result.answerNote && <p className="text-muted-foreground text-sm">{result.answerNote}</p>}
      {result.copyAnswer && (
        <div>
          <Button type="button" variant="outline" onClick={() => void copy(result.body)}>
            {copied ? "Copied" : "Copy answer"}
          </Button>
          {failed && <p role="alert" className="text-destructive text-sm">{"Couldn't copy. Select the text and copy it yourself."}</p>}
        </div>
      )}
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        {result.imageModel && <Fact label="Model" value={result.imageModel} />}
        <Fact
          label={result.imageModel ? "Run time" : job ? "Request time" : "Time"}
          value={`${result.elapsedMs} ms`}
        />
        {result.cost && <Fact label="Cost" value={result.cost} />}
        <Fact
          label="Receipt"
          value={
            result.checkingReceipt ? (
              "Checking receipt..."
            ) : result.receiptId ? (
              <ReceiptLink id={result.receiptId} />
            ) : result.receiptLookup ? (
              // The check failed, which says nothing about the receipt.
              <span className="flex flex-wrap items-center gap-x-2">
                Receipt couldn’t be checked.
                <button
                  type="button"
                  className="min-h-11 underline underline-offset-4 disabled:opacity-50"
                  disabled={result.checkingReceipt}
                  onClick={onCheckReceipt}
                >
                  {result.checkingReceipt ? "Checking…" : "Check again"}
                </button>
              </span>
            ) : (
              "No receipt was returned."
            )
          }
        />
      </dl>
      {result.technical && <TechnicalDetails text={result.technical} />}
      <details className="space-y-2 border-t pt-3">
        <summary className="flex min-h-11 cursor-pointer items-center gap-2 text-sm font-medium">
          <Copy aria-hidden="true" className="size-4" />
          Copy request as
        </summary>
        <SnippetBlock snippets={codeSnippets(result.request)} />
      </details>
    </div>
  );
}

/** Raw answers and status codes, folded away from the normal view (P15). */
function TechnicalDetails({ text }: { text: string }) {
  return (
    <details className="border-t pt-3">
      <summary className="min-h-11 cursor-pointer text-sm font-medium">
        Technical details
      </summary>
      <pre
        tabIndex={0}
        className="bg-muted max-h-80 overflow-auto rounded-lg p-3 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {text}
      </pre>
    </details>
  );
}

const MEDIA_NOUN = { image: "picture", audio: "audio", video: "video" } as const;

/**
 * One generated picture, sound or video, with its own wait and failure
 * (PF-009). Retry loads the same address again.
 */
function ResultMedia({
  kind,
  url,
  label,
}: {
  kind: "image" | "audio" | "video";
  url: string;
  label?: string;
}) {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [attempt, setAttempt] = useState(0);
  const noun = MEDIA_NOUN[kind];
  const events = {
    onError: () => setState("error"),
    ...(kind === "image"
      ? { onLoad: () => setState("ready") }
      : { onLoadedMetadata: () => setState("ready") }),
  };
  if (state === "error")
    return (
      <div role="status" className="flex flex-wrap items-center gap-x-3 rounded-lg border p-3 text-sm">
        The {noun} couldn’t be loaded.
        <button
          type="button"
          className="min-h-11 underline underline-offset-4"
          onClick={() => {
            setAttempt((value) => value + 1);
            setState("loading");
          }}
        >
          Retry
        </button>
      </div>
    );
  return (
    <div className="space-y-1" aria-busy={state === "loading"}>
      {kind === "image" ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          key={attempt}
          src={url}
          alt="Generated result"
          className="max-h-96 w-full rounded-lg border object-contain"
          {...events}
        />
      ) : kind === "audio" ? (
        <audio
          key={attempt}
          aria-label={label}
          controls
          src={url}
          className="w-full"
          {...events}
        >
          <a href={url}>Download audio</a>
        </audio>
      ) : (
        <video
          key={attempt}
          aria-label={label}
          controls
          src={url}
          className="max-h-96 w-full rounded-lg"
          {...events}
        />
      )}
      {state === "loading" && (
        <p role="status" className="text-muted-foreground text-sm">
          Loading {noun === "audio" ? "the audio" : `the ${noun}`}…
        </p>
      )}
    </div>
  );
}

/**
 * "Don't keep this prompt", set before Run (ChatGPT's Temporary Chat and
 * Safari's private mode: the mode is chosen before you send, and the screen
 * shows it). One row above Run: the switch and what happens to the words.
 * When memory is off for the person or the household there is no choice
 * to make, so the row says so instead of offering a switch that does
 * nothing.
 */
function PromptKeepControl({
  id,
  state,
  forget,
  onForget,
  owner,
  made,
}: {
  id: string;
  state: TryKeepState;
  forget: boolean;
  onForget: (next: boolean) => void;
  owner: boolean;
  made: string;
}) {
  const fixed = state === "member-off" || state === "household-off";
  const notKept = fixed || forget;
  const Icon = notKept ? EyeOff : History;
  const label = fixed ? "Prompts aren’t kept" : "Don’t keep this prompt";
  const description = fixed ? (
    state === "member-off" ? (
      <>
        You turned off prompt keeping.{" "}
        <Link prefetch={false}
          href="/account"
          className="text-foreground underline underline-offset-4"
        >
          Change
        </Link>
      </>
    ) : owner ? (
      <>
        You turned off prompt keeping for everyone.{" "}
        <Link prefetch={false}
          href="/settings#prompts"
          className="text-foreground underline underline-offset-4"
        >
          Settings
        </Link>
      </>
    ) : (
      "Turned off for everyone on this gateway."
    )
  ) : forget ? (
    `The ${made} still goes to Library.`
  ) : state === "unknown" ? (
    "Couldn't read your prompt settings. Saved prompts are visible only to you."
  ) : (
    <>
      <span className="block">
        Azizam tries to keep the prompt with the {made}.
      </span>
      <span className="block">Only you can see kept prompts.</span>
    </>
  );
  return (
    <div
      data-slot="prompt-keep"
      data-state={notKept ? "not-kept" : "kept"}
      className={cn(
        "rounded-control flex items-center gap-3 border px-3 py-2.5",
        notKept && "bg-muted",
      )}
    >
      <Icon
        aria-hidden="true"
        className="text-muted-foreground size-4 shrink-0"
      />
      <div className="min-w-0 flex-1">
        {fixed ? (
          <p className="text-body font-medium">{label}</p>
        ) : (
          <label htmlFor={id} className="text-body block font-medium">
            {label}
          </label>
        )}
        <p id={`${id}-note`} className="text-muted-foreground text-meta">
          {description}
        </p>
      </div>
      {fixed ? null : (
        <Switch
          id={id}
          checked={forget}
          onCheckedChange={onForget}
          aria-describedby={`${id}-note`}
        />
      )}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border p-3">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="mt-1 font-medium break-words">{value}</dd>
    </div>
  );
}

function SnippetBlock({
  snippets,
}: {
  snippets: Array<{ language: string; code: string }>;
}) {
  const firstLanguage = snippets[0]?.language ?? "";
  const [activeLanguage, setActiveLanguage] = useState(firstLanguage);
  const selectedLanguage = snippets.some((snippet) => snippet.language === activeLanguage)
    ? activeLanguage
    : firstLanguage;
  const { copied, failed, copy } = useCopied(JSON.stringify([selectedLanguage, snippets]));
  return (
    <Tabs
      value={selectedLanguage}
      onValueChange={(language) => {
        setActiveLanguage(language);
      }}
      className="bg-muted rounded-lg border"
    >
      <div className="flex flex-col gap-2 border-b p-2 sm:flex-row sm:items-center sm:justify-between">
        <TabsList aria-label="Snippet format" className="w-full sm:w-auto">
          {snippets.map((snippet) => (
            <TabsTrigger key={snippet.language} value={snippet.language}>
              {snippet.language}
            </TabsTrigger>
          ))}
        </TabsList>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="min-h-11 self-end sm:self-auto"
          onClick={() => {
            const snippet =
              snippets.find((item) => item.language === selectedLanguage) ??
              snippets[0];
            if (snippet) void copy(snippet.code);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {failed && <p role="alert" className="text-destructive p-3 text-sm">{"Couldn't copy. Select the text and copy it yourself."}</p>}
      {snippets.map((snippet) => (
        <TabsContent
          key={snippet.language}
          value={snippet.language}
          className="m-0"
        >
          <pre
            tabIndex={0}
            className="overflow-x-auto p-3 font-mono text-xs leading-relaxed"
          >
            <code>{snippet.code}</code>
          </pre>
        </TabsContent>
      ))}
    </Tabs>
  );
}

function ModelSelect({
  loading,
  value,
  options,
  onChange,
}: {
  loading: boolean;
  value: string;
  options: CatalogOption[];
  onChange: (value: string) => void;
}) {
  if (loading) return (
    <div role="status" aria-label="Loading models" aria-busy="true">
      <select aria-label="Model" disabled className="border-input bg-muted rounded-control min-h-11 w-full border px-3">
        <option>Loading models…</option>
      </select>
    </div>
  );
  const values = [
    ...(value && !options.some((option) => option.id === value)
      ? [
          {
            id: value,
            // Only a link puts a model here that the list does not have.
            label: `${modelDisplayName(value)} · from your link, not in your list`,
            price: "",
            locality: "Not in the catalog",
          },
        ]
      : []),
    ...options,
  ];
  return (
    <Select
      value={value}
      disabled={!values.length}
      onValueChange={(next) => {
        // Catalog updates can reset the Select's hidden form input. A model
        // must keep its selected identifier rather than accept that empty value.
        if (next) onChange(next);
      }}
    >
      <SelectTrigger
        aria-label="Model"
        className="w-full min-w-0 overflow-hidden [&_[data-slot=select-value]]:min-w-0"
      >
        <SelectValue placeholder="No options available" />
      </SelectTrigger>
      <SelectContent>
        {values.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            {[option.label, option.price.startsWith("Paid") ? option.price : ""]
              .filter(Boolean)
              .join(" · ")}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function CatalogNotice({
  state,
  section,
  empty,
  owner,
  unknownModelCount = 0,
  onRetry,
}: {
  state: CatalogState;
  section: CatalogSection;
  /** What the empty list holds ("chat models"), when this task has none. */
  empty: string | null;
  owner: boolean;
  unknownModelCount?: number;
  onRetry: () => void;
}) {
  const noun = section === "workflows" ? "workflow" : "model";
  const unsupported = unknownModelCount ? (
    <Badge variant="outline">
      {unknownModelCount} model{unknownModelCount === 1 ? " has" : "s have"} no
      supported Try capability and cannot be selected automatically.
    </Badge>
  ) : null;
  // ErrorState action buttons must not submit the surrounding Run form.
  if (state === "error")
    return (
      <div onClick={(event) => event.preventDefault()}>
        <ErrorState
          heading={`${noun === "model" ? "Models" : "Workflows"} couldn’t be loaded`}
          message={`We couldn’t tell why the ${noun} list didn’t load.`}
          action={{ label: "Retry", onClick: onRetry }}
        />
        {unsupported}
      </div>
    );
  const none = empty ? (
    <p role="status" className="text-muted-foreground text-sm">
      No {empty} are available
      {owner ? (
        <>
          .{" "}
          <Link prefetch={false} className="text-foreground underline underline-offset-4" href="/engines/">
            Add one in Engines
          </Link>
          .
        </>
      ) : (
        ". Ask the owner."
      )}
    </p>
  ) : null;
  if (unsupported || none)
    return (
      <>
        {none}
        {unsupported}
      </>
    );
  return null;
}

/**
 * The link asked for a kind of work (video, music, sound effects) that Try
 * runs through workflows. Only workflows of that kind are offered, and the
 * line says so, with a way back to all of them.
 */
function KindNotice({
  kind,
  matches,
  others,
  owner,
  onShowAll,
}: {
  kind: RequestedKind;
  matches: number;
  /** Other workflows exist beyond this kind. */
  others: boolean;
  owner: boolean;
  onShowAll: () => void;
}) {
  const showAll = others ? (
    <button
      type="button"
      className="text-foreground min-h-11 underline underline-offset-4"
      onClick={onShowAll}
    >
      Show all workflows
    </button>
  ) : null;
  if (!matches)
    return (
      <div role="status" className="text-muted-foreground space-y-1 text-sm">
        <p>
          {owner ? (
            <>
              No {kind.noun} engine is set up.{" "}
              <Link prefetch={false} className="text-foreground underline underline-offset-4" href="/engines/">
                Add one in Engines
              </Link>
              .
            </>
          ) : (
            `No ${kind.noun} ${kind.plural ? "are" : "is"} available to you right now. Ask the owner about ${kind.noun}.`
          )}
        </p>
        {showAll}
      </div>
    );
  return (
    <p role="status" className="text-muted-foreground text-sm">
      {kind.capabilities.length > 1
        ? "Choose an audio workflow to make music or sound effects."
        : `Showing ${kind.noun} workflows.`}{" "}
      {showAll}
    </p>
  );
}
