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
  GitBranch,
  Image as ImageIcon,
  Loader2,
  Mic,
  Pause,
  Play,
  Layers,
} from "lucide-react";
import {
  WorkflowInputs,
  parseWorkflowInputs,
  workflowDefaults,
  type WorkflowInput,
  type WorkflowInputMapping,
} from "./workflow-inputs";
import {
  mediaFrom,
  workflowJob,
  type WorkflowMedia,
  type WorkflowJob,
} from "./workflow-media";
import { costFrom } from "./result-cost";
import { ReceiptLink } from "./receipt-link";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import { availabilityReason } from "@/lib/status-copy";
import { Button } from "@/components/ui/button";
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
  status?: number;
  receiptId?: string | null;
  elapsedMs?: number;
};
type RequestInfo = {
  path: string;
  body: unknown;
  multipart?: boolean;
  form?: Record<string, unknown>;
  files?: WorkflowExampleFile[];
};
type WorkflowExampleFile = {
  field: string;
  placeholder: string;
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
};
type WorkflowOption = {
  id: string;
  short_id?: string;
  title: string;
  description?: string;
  availability?: { available?: boolean; status?: string; reason_code?: string };
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
    files?: WorkflowExampleFile[];
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
  elapsedMs: number;
  cost?: string;
  receiptId: string | null;
  request: RequestInfo;
  imageUrl?: string;
  audioUrl?: string;
  downloadUrl?: string;
  downloadName?: string;
  vector?: { dimensions: number; min: number; max: number; mean: number };
  media?: WorkflowMedia[];
  job?: WorkflowJob;
  technical?: string;
  probabilities?: Array<{ option: string; probability: number | null }>;
};

const DEFAULTS: Record<TryTab, { model: string; prompt: string }> = {
  chat: { model: "chat", prompt: "Reply with one short sentence." },
  image: { model: "image", prompt: "A calm teal fish mark on a white card." },
  speech: { model: "tts", prompt: "The gateway is ready." },
  transcribe: { model: "stt", prompt: "" },
  decide: {
    model: "decide",
    prompt: "Which option should run first for a smoke test?",
  },
  embed: {
    model: "embeddings",
    prompt: "Gateway receipts are useful evidence.",
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
    label: "Chat",
    icon: MessageSquare,
    help: "",
  },
  {
    value: "image",
    label: "Image",
    icon: ImageIcon,
    help: "Generate one image from a prompt.",
  },
  {
    value: "speech",
    label: "Speech",
    icon: AudioLines,
    help: "Turn a sentence into audio.",
  },
  {
    value: "transcribe",
    label: "Transcription",
    icon: Mic,
    help: "Upload or record audio and get text.",
  },
  {
    value: "decide",
    label: "Decisions",
    icon: GitBranch,
    help: "Choose an option based on the information you provide.",
  },
  {
    value: "embed",
    label: "Embeddings",
    icon: Layers,
    help: "Turn text into numbers for search.",
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
  audio: "workflow",
  music: "workflow",
  sfx: "workflow",
  workflow: "workflow",
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
}

async function publicRequest(path: string, key: string, init: RequestInit) {
  const started = performance.now();
  const response = await fetch(publicPath(path), {
    ...init,
    headers: { authorization: `Bearer ${key}`, ...(init.headers ?? {}) },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
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
        : `Request failed with HTTP ${response.status}.`;
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

async function verifiedReceiptId(response: Response, payload: unknown) {
  async function verify(requestId: string | null) {
    if (!requestId) return null;
    const receipt = await fetch(
      `/me/api/receipts/${encodeURIComponent(requestId)}`,
      { credentials: "same-origin" },
    );
    return receipt.ok ? requestId : null;
  }
  try {
    const direct = await verify(receiptFrom(response, payload));
    if (direct) return direct;
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
    const ids = [
      ...new Set(
        items
          .map((item) => item?.asset_id)
          .filter(
            (id): id is string => typeof id === "string" && id.length > 0,
          ),
      ),
    ].slice(0, 10);
    for (const id of ids) {
      const detail = await fetch(`/me/api/media/${encodeURIComponent(id)}`, {
        credentials: "same-origin",
      });
      if (!detail.ok) continue;
      const value = (await detail.json()) as {
        asset?: { request_id?: unknown };
      };
      const requestId =
        typeof value.asset?.request_id === "string"
          ? value.asset.request_id
          : null;
      const verified = await verify(requestId);
      if (verified) return verified;
    }
  } catch {
    return null;
  }
  return null;
}

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
    const files = request.files?.filter(
      (file) => file.required !== false || file.selected,
    ) ?? [{ field: "file", placeholder: "sample.wav" }];
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
        `  -F ${shellQuote(`${file.field}=@${file.placeholder}${file.type ? `;type=${file.type}` : ""}`)}`,
    );
    const pythonFiles = files
      .map(
        (file) =>
          `    ${JSON.stringify(file.field)}: (${JSON.stringify(file.placeholder)}, stack.enter_context(open(${JSON.stringify(file.placeholder)}, "rb")), ${JSON.stringify(file.type ?? "application/octet-stream")}),`,
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
          `form.set(${JSON.stringify(file.field)}, new Blob([await fs.readFile(${JSON.stringify(file.placeholder)})], {type: ${JSON.stringify(file.type ?? "application/octet-stream")}}), ${JSON.stringify(file.placeholder)});`,
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
        code: `import os\nimport json\nimport requests\nfrom contextlib import ExitStack\n\nwith ExitStack() as stack:\n    files = {\n${pythonFiles}\n    }\n    data = json.loads(${JSON.stringify(JSON.stringify(Object.fromEntries(fields)))})\n    response = requests.post(${JSON.stringify(url)}, headers={"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}, data=data, files=files)\n    print(response.status_code)\n    open("workflow-result", "wb").write(response.content)`,
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
  return choice?.message?.content ?? JSON.stringify(payload, null, 2);
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
    return {};
  }
  const records = [
    ...(payload as { aliases: unknown[] }).aliases,
    ...(payload as { models: unknown[] }).models,
  ];
  const grouped: Partial<Record<TryTab, CatalogOption[]>> = {};
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
        label:
          stringValue(record.name) ?? stringValue(record.display_name) ?? id,
        recommended: record.recommended === true,
        capability,
        price: costLabel(record.cost),
        locality: localityLabel(record),
        provider: stringValue(record.provider) ?? undefined,
      };
      if (!(grouped[capability] ?? []).some((existing) => existing.id === id)) {
        grouped[capability] = [...(grouped[capability] ?? []), option];
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
              label: [option.label, option.provider, option.id]
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
    return [];
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
      label: `${workflow.title || workflow.id}${workflow.availability?.available === false ? " · Unavailable" : ""}`,
      capability: "workflow" as const,
      price: "",
      locality: "Workflow manifest",
      workflow,
    });
  }
  return options;
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
    files: workflowExampleFiles(record.files),
    curl: stringValue(record.curl) ?? undefined,
    python: stringValue(record.python) ?? undefined,
    javascript: stringValue(record.javascript) ?? undefined,
  };
}

function workflowExampleFiles(value: unknown): WorkflowExampleFile[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [];
  }
  return Object.entries(value as Record<string, unknown>)
    .map(([field, placeholder]) => ({
      field,
      placeholder: String(placeholder ?? ""),
      required: true,
    }))
    .filter((file) => file.field);
}

function workflowFileSpecs(workflow: WorkflowOption): WorkflowExampleFile[] {
  const files = [...(workflow.example_request?.files ?? [])];
  if (requestIsMultipart(workflow)) {
    for (const input of workflow.inputs) {
      const mapping = workflow.inputMapping?.[input.name];
      const location = mapping?.in ?? input.applies?.toLowerCase();
      if (input.ignored || input.accepted === false) continue;
      if (
        location?.includes("file") ||
        (!location && ["asset", "file", "binary"].includes(input.type))
      ) {
        const field = mapping?.field ?? input.request_field ?? input.name;
        if (!files.some((file) => file.field === field))
          files.push({ field, placeholder: `./${field}.bin` });
      }
    }
  }
  return files.map((file) => {
    const input = workflow.inputs.find(
      (item) =>
        item.name === file.field ||
        item.request_field === file.field ||
        workflow.inputMapping?.[item.name]?.field === file.field,
    );
    return {
      ...file,
      required: input ? input.required === true : file.required !== false,
    };
  });
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
    if (tab) tabs.add(tab);
  }
  return [...tabs];
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
  tab: TryTab,
  current: string,
  options: CatalogOption[],
  explicit: boolean,
) {
  if (!options.length) return current;
  if (explicit) return current;
  const recommended = options.find((option) => option.recommended);
  if (
    current &&
    current !== DEFAULTS[tab].model &&
    options.some((option) => option.id === current)
  )
    return current;
  if (
    !current ||
    current === DEFAULTS[tab].model ||
    !options.some((option) => option.id === current)
  )
    return recommended?.id ?? options[0].id;
  return current;
}

export default function TryPage() {
  const searchParams = useSearchParams();
  const requestedWorkflow = searchParams.get("workflow");
  const requestedCapability = searchParams.get("capability");
  const requestedModel = searchParams.get("model") ?? searchParams.get("alias");
  const requestedCapabilityKey = requestedCapability?.toLowerCase().trim();
  const [key, setKey] = useState<TryKey | null>(null);
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TryTab>("chat");
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
  const [voice, setVoice] = useState("alloy");
  const [decisionState, setDecisionState] = useState(
    "Gateway smoke test is starting.",
  );
  const [options, setOptions] = useState("Run chat\nRun embeddings");
  const [file, setFile] = useState<File | null>(null);
  const [workflowFiles, setWorkflowFiles] = useState<
    Record<string, File | null>
  >({});
  const [checkingJob, setCheckingJob] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);
  const [recordingError, setRecordingError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const objectUrls = useRef<string[]>([]);
  const mounted = useRef(true);
  const activeTabRef = useRef<TryTab>("chat");
  const explicitModelTabs = useRef<Set<TryTab>>(new Set());
  const initializedWorkflowId = useRef<string | null>(null);
  const chunks = useRef<Blob[]>([]);
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
  const parsedOptions = useMemo(
    () =>
      options
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
    [options],
  );
  const selectedWorkflow = useMemo(
    () =>
      catalog.workflow?.find((option) => option.id === model.workflow)
        ?.workflow,
    [catalog.workflow, model.workflow],
  );
  useEffect(() => {
    mounted.current = true;
    const urls = objectUrls.current;
    return () => {
      mounted.current = false;
      if (recorder.current?.state === "recording") recorder.current.stop();
      stream.current?.getTracks().forEach((track) => track.stop());
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    const tab =
      requestedCapability == null
        ? null
        : CAPABILITY_TABS[requestedCapability.toLowerCase().trim()];
    if (tab) setActiveTab(tab);
  }, [requestedCapability]);

  useEffect(() => {
    if (!requestedModel || requestedCapabilityKey === "audio") return;
    const tab =
      requestedCapability == null
        ? "chat"
        : (CAPABILITY_TABS[requestedCapability.toLowerCase().trim()] ?? "chat");
    explicitModelTabs.current.add(tab);
    setModel((current) => ({ ...current, [tab]: requestedModel }));
  }, [requestedCapability, requestedCapabilityKey, requestedModel]);

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
  }, [selectedWorkflow]);

  useEffect(() => {
    activeTabRef.current = activeTab;
    if (activeTab !== "transcribe") stopRecording();
  }, [activeTab]);

  async function prepareKey() {
    setKeyBusy(true);
    setKeyError(null);
    try {
      const account = await accountApi.session();
      if (!account.authenticated)
        throw new Error("Sign in to create a Try key.");
      let app: AccountApp | undefined = account.apps.find(
        (item) => item.name.toLowerCase() === "try",
      );
      app ??= await accountApi.createApp("Try");
      const minted = await accountApi.mintKey(app.id, "Try session");
      setKey({ id: minted.id, secret: minted.api_key, appName: app.name });
      const preparedCatalog = await loadCatalog(minted.api_key);
      return {
        id: minted.id,
        secret: minted.api_key,
        appName: app.name,
        catalog: preparedCatalog,
      };
    } catch (error) {
      setKeyError(
        error instanceof Error
          ? error.message
          : "Could not prepare a Try key. Try again.",
      );
      return null;
    } finally {
      setKeyBusy(false);
    }
  }

  async function revokeTryKey() {
    if (!key) return;
    setKeyBusy(true);
    setKeyError(null);
    try {
      await accountApi.revokeKey(key.id);
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
      const mediaRecorder = new MediaRecorder(mediaStream);
      chunks.current = [];
      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      mediaRecorder.onstop = () => {
        acquiredStream.getTracks().forEach((track) => track.stop());
        stream.current = null;
        const blob = new Blob(chunks.current, { type: "audio/webm" });
        setFile(new File([blob], "recording.webm", { type: "audio/webm" }));
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
    for (const tab of TAB_ITEMS) {
      const options = grouped[tab.value] ?? [];
      next[tab.value] = selectedOrDefaultModel(
        tab.value,
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

  async function loadCatalogSection(section: CatalogSection, secret?: string) {
    catalogRequests.current[section]?.abort();
    const controller = new AbortController();
    catalogRequests.current[section] = controller;
    const current = () =>
      mounted.current &&
      !controller.signal.aborted &&
      catalogRequests.current[section] === controller;
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
      if (section === "workflows") {
        if (!response.ok) throw new Error("Workflow catalog unavailable");
        let workflowOptions = groupWorkflows(await response.json());
        if (requestedCapabilityKey === "audio" && !requestedWorkflow) {
          workflowOptions = workflowOptions.filter((option) =>
            option.workflow?.outputs?.some(
              (output) =>
                output.kind === "audio" ||
                output.type === "audio" ||
                output.media_type?.startsWith("audio/"),
            ),
          );
        }
        grouped = { workflow: workflowOptions };
      } else if (response.ok) {
        grouped = groupCatalog(await response.json());
      } else if (secret) {
        const fallback = await fetch("/openai/v1/models", {
          headers: { authorization: `Bearer ${secret}` },
          signal: controller.signal,
        });
        if (!fallback.ok) throw new Error("Model catalog unavailable");
        const payload = (await fallback.json()) as {
          data?: Array<{ id?: string }>;
        };
        grouped = {
          chat: (payload.data ?? []).flatMap((item) =>
            item.id
              ? [
                  {
                    id: item.id,
                    label: item.id,
                    capability: "chat" as const,
                    price: "",
                    locality: "Available to this Try key",
                  },
                ]
              : [],
          ),
        };
      } else {
        throw new Error("Model catalog unavailable");
      }
      if (!current()) return;
      const merged =
        section === "workflows"
          ? { ...catalogRef.current, workflow: grouped.workflow }
          : { ...grouped, workflow: catalogRef.current.workflow };
      catalogRef.current = merged;
      setCatalog(merged);
      setModel((selected) => nextModelForCatalog(merged, selected));
      const hasOptions = Object.values(grouped).some(
        (options) => options?.length,
      );
      setCatalogState((states) => ({
        ...states,
        [section]: hasOptions ? "ready" : "empty",
      }));
    } catch {
      if (current())
        setCatalogState((states) => ({ ...states, [section]: "error" }));
    }
  }

  function buildRequest(
    tab: TryTab,
    selectedModel: Record<TryTab, string> = model,
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
      return {
        path: "/audio/speech",
        body: { model: selectedModel.speech, voice, input: prompt.speech },
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
      const route = selectedWorkflow?.example_request?.path;
      if (!route) {
        throw new Error(
          "This workflow does not include a runnable example request yet.",
        );
      }
      const multipart = requestIsMultipart(selectedWorkflow);
      const values = parseWorkflowInputs(
        prompt.workflow || "{}",
        selectedWorkflow.inputs,
        selectedWorkflow.inputMapping,
      );
      const body = multipart ? {} : values;
      const formBody = multipart ? values : {};
      if (multipart) {
        for (const input of selectedWorkflow.inputs) {
          const mapping = selectedWorkflow.inputMapping?.[input.name];
          if (
            mapping?.in !== "file+extension" ||
            !mapping.field ||
            !workflowFiles[mapping.field]
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
        body.model = selectedWorkflow.short_id ?? selectedWorkflow.id;
      }
      return {
        path: route,
        body,
        form: formBody,
        files: workflowFileSpecs(selectedWorkflow).map((file) => ({
          ...file,
          placeholder: workflowFiles[file.field]?.name ?? file.placeholder,
          type: workflowFiles[file.field]?.type || file.type,
          selected: Boolean(workflowFiles[file.field]),
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
    if (tab === "workflow" && selectedWorkflow?.availability?.available === false)
      return;
    let runKey = key;
    let preparedCatalog: CatalogGroups | null = null;
    if (!runKey?.secret) {
      const prepared = await prepareKey();
      runKey = prepared;
      preparedCatalog = prepared?.catalog ?? null;
      if (!runKey?.secret) return;
    }
    const selectedModel = preparedCatalog
      ? nextModelForCatalog(preparedCatalog, model)
      : model;
    setRunning(tab);
    if (tab === "workflow") setJobError(null);
    setErrors((current) => ({ ...current, [tab]: undefined }));
    try {
      const request = buildRequest(tab, selectedModel);
      let result: Awaited<ReturnType<typeof publicRequest>>;
      if (request.multipart) {
        const form = new FormData();
        if (tab === "workflow") {
          for (const fileSpec of request.files ?? []) {
            const field = fileSpec.field;
            const selectedFile = workflowFiles[field];
            if (!selectedFile && fileSpec.required !== false) {
              throw new Error(`Choose a file for workflow input ${field}.`);
            }
            if (selectedFile) {
              form.set(field, selectedFile);
            }
          }
          for (const [name, value] of Object.entries(request.form ?? {})) {
            if (value === undefined || value === null) continue;
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
      const receiptId = await verifiedReceiptId(
        result.response,
        result.payload,
      );
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
        [tab]: formatResult(
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
          receiptId,
          request,
        ),
      }));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [tab]: {
          message:
            error instanceof ApiError && tab === "decide" && error.status === 404
              ? "Decisions not enabled on this gateway"
              : error instanceof Error
                ? error.message
                : "This request could not be completed.",
          ...(error instanceof PublicRequestError
            ? {
                status: error.status,
                receiptId: receiptFrom(error.response, error.payload),
                elapsedMs: error.elapsedMs,
              }
            : {}),
        },
      }));
    } finally {
      setRunning(null);
    }
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
      const receiptId = requestId
        ? await verifiedReceiptId(
            new Response(null, {
              headers: { "x-azizam-receipt-id": requestId },
            }),
            null,
          )
        : null;
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
          receiptId,
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
    if (tab === "chat") {
      return {
        title: "Chat result",
        body: textFromChat(payload),
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    if (tab === "image") {
      const imageUrl = imageFrom(payload);
      return {
        title: "Image result",
        body: imageUrl ? "Image generated." : JSON.stringify(payload, null, 2),
        imageUrl,
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    if (tab === "speech") {
      const audioUrl =
        payload instanceof Blob ? URL.createObjectURL(payload) : undefined;
      if (audioUrl) {
        objectUrls.current.push(audioUrl);
      }
      return {
        title: "Speech result",
        body: audioUrl ? "Audio generated." : JSON.stringify(payload, null, 2),
        audioUrl,
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    if (tab === "transcribe") {
      return {
        title: "Transcription result",
        body:
          (payload as { text?: string } | null)?.text ??
          JSON.stringify(payload, null, 2),
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    if (tab === "decide") {
      return {
        title: "Decision result",
        body: JSON.stringify(payload, null, 2),
        probabilities: decisionRows(payload, parsedOptions),
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    if (tab === "workflow") {
      const media = workflowOutputMedia(payload);
      const job =
        workflowJob(payload, selectedWorkflow?.jobRoutes) ?? undefined;
      return {
        title: "Workflow result",
        body: job
          ? `Job ${job.status}.`
          : media.length
            ? `${media.length} output${media.length === 1 ? "" : "s"} returned.`
            : "Workflow returned a response. Open Technical details to inspect it.",
        media,
        job,
        technical:
          payload instanceof Blob
            ? `${payload.type ? `${payload.type} · ` : ""}${payload.size} bytes`
            : JSON.stringify(payload, null, 2),
        elapsedMs,
        cost,
        receiptId,
        request,
      };
    }
    const vector = embeddingStats(payload);
    return {
      title: "Embedding result",
      body: vector ? "Vector created." : JSON.stringify(payload, null, 2),
      vector: vector ?? undefined,
      elapsedMs,
      cost,
      receiptId,
      request,
    };
  }

  return (
    <div className="space-y-8">
      <PageHeader title="Try" />

      <section className="text-muted-foreground text-sm">
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
                Creating Try key
              </span>
            )}
          </div>
        ) : (
          <p>
            Using a Try key
            <span aria-hidden="true"> · </span>
            <button
              type="button"
              className="text-foreground underline underline-offset-4 disabled:opacity-50"
              onClick={() => void revokeTryKey()}
              disabled={keyBusy}
            >
              Revoke
            </button>
          </p>
        )}
      </section>

      <Tabs
        value={activeTab}
        onValueChange={(value) => setActiveTab(value as TryTab)}
      >
        <div
          className="overflow-x-auto pb-1"
          tabIndex={0}
          aria-label="Try capability tabs"
        >
          <TabsList aria-label="Try capability" className="min-w-max">
            {TAB_ITEMS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                <tab.icon aria-hidden="true" />
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {TAB_ITEMS.map((tab) => {
          const currentResult = results[tab.value];
          const currentError = errors[tab.value];
          return (
            <TabsContent key={tab.value} value={tab.value} className="mt-4">
              <div className="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <form
                  noValidate
                  onSubmit={(event) => void run(tab.value, event)}
                  className="bg-card min-w-0 space-y-4 rounded-xl border p-5"
                >
                  <div>
                    <h2 className="flex items-center gap-2 font-semibold">
                      <tab.icon aria-hidden="true" className="size-4" />
                      {tab.label}
                    </h2>
                    {tab.help && (
                      <p className="text-muted-foreground mt-1 text-sm">
                        {tab.help}
                      </p>
                    )}
                  </div>
                  <label className="block space-y-2">
                    <span className="text-sm font-medium">Model name</span>
                    <ModelSelect
                      value={model[tab.value]}
                      fallbackLabel={DEFAULTS[tab.value].model}
                      options={catalog[tab.value] ?? []}
                      onChange={(value) =>
                        setModel((current) => ({
                          ...current,
                          [tab.value]: value,
                        }))
                      }
                    />
                  </label>
                  {tab.value === activeTab && (
                    <CatalogNotice
                      state={
                        catalogState[
                          tab.value === "workflow" ? "workflows" : "models"
                        ]
                      }
                      section={
                        tab.value === "workflow" ? "workflows" : "models"
                      }
                      onRetry={() =>
                        void loadCatalogSection(
                          tab.value === "workflow" ? "workflows" : "models",
                          key?.secret,
                        )
                      }
                    />
                  )}
                  {tab.value === "workflow" &&
                    requestedCapabilityKey === "audio" && (
                      <p
                        role="status"
                        className="text-muted-foreground text-sm"
                      >
                        Audio has no dedicated Try panel. Choose a workflow if
                        this gateway exposes one.
                      </p>
                    )}
                  {tab.value === "decide" && (
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">State</span>
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
                      inputs={selectedWorkflow.inputs}
                      mapping={selectedWorkflow.inputMapping}
                      json={prompt.workflow}
                      onChange={(json) =>
                        setPrompt((current) => ({ ...current, workflow: json }))
                      }
                    />
                  )}
                  {tab.value === "workflow" && selectedWorkflow && (
                    <WorkflowFileInputs
                      workflow={selectedWorkflow}
                      files={workflowFiles}
                      onChange={(field, nextFile) =>
                        setWorkflowFiles((current) => ({
                          ...current,
                          [field]: nextFile,
                        }))
                      }
                    />
                  )}
                  {tab.value === "workflow" && (
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
                    <details className="border-t pt-3">
                      <summary className="min-h-11 cursor-pointer text-sm font-medium">
                        More options
                      </summary>
                      <label className="mt-3 block space-y-2">
                        <span className="text-sm font-medium">Voice</span>
                        <Input
                          value={voice}
                          onChange={(event) => setVoice(event.target.value)}
                        />
                      </label>
                    </details>
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
                  <Button
                    type="submit"
                    variant="outline"
                    disabled={
                      running !== null ||
                      keyBusy ||
                      checkingJob ||
                      (tab.value === "workflow" &&
                        (!selectedWorkflow ||
                          selectedWorkflow.availability?.available === false))
                    }
                  >
                    {running === tab.value ? (
                      <Loader2 aria-hidden="true" className="animate-spin" />
                    ) : (
                      <Play aria-hidden="true" />
                    )}
                    Run
                  </Button>
                  {tab.value === "workflow" &&
                    selectedWorkflow?.availability?.available === false && (
                      <p className="text-muted-foreground text-sm">
                        {availabilityReason(
                          selectedWorkflow.availability.status ?? "unavailable",
                          selectedWorkflow.availability.reason_code,
                        ) ?? "Unavailable."}
                      </p>
                    )}
                </form>
                <section
                  className="bg-card min-w-0 rounded-xl border p-5"
                  aria-live="polite"
                >
                  {running === tab.value ? (
                    <div aria-busy="true" className="space-y-3">
                      <Skeleton className="h-6 w-40" />
                      <Skeleton className="h-28 w-full" />
                      <Skeleton className="h-20 w-full" />
                    </div>
                  ) : currentError ? (
                    <div className="space-y-5">
                      <ErrorState
                        message={currentError.message}
                        onRetry={() => void loadCatalog(key?.secret)}
                      />
                      {currentError.status !== undefined && (
                        <dl className="grid gap-3 text-sm sm:grid-cols-3">
                          <Fact label="Status" value={`HTTP ${currentError.status}`} />
                          <Fact label="Time" value={`${currentError.elapsedMs} ms`} />
                          {currentError.receiptId && (
                            <Fact
                              label="Receipt"
                              value={<ReceiptLink id={currentError.receiptId} />}
                            />
                          )}
                        </dl>
                      )}
                    </div>
                  ) : currentResult ? (
                    <ResultView
                      result={currentResult}
                      onCheckJob={() => void checkWorkflowJob()}
                      checkingJob={checkingJob}
                      jobError={jobError}
                    />
                  ) : (
                    <EmptyState
                      icon={tab.icon}
                      title={`Run ${tab.label.toLowerCase()}`}
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
  onCheckJob,
  checkingJob,
  jobError,
}: {
  result: Result;
  onCheckJob: () => void;
  checkingJob: boolean;
  jobError: string | null;
}) {
  const probabilityRows = result.probabilities?.filter(
    (row): row is { option: string; probability: number } =>
      row.probability !== null,
  );
  return (
    <div className="min-w-0 space-y-5">
      <div className="space-y-2">
        <h2 className="font-semibold">{result.title}</h2>
        {Boolean(result.media?.length) && (
          <p className="text-muted-foreground text-sm">
            {result.media?.length} output{result.media?.length === 1 ? "" : "s"}{" "}
            returned.
          </p>
        )}
        {result.imageUrl && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={result.imageUrl}
            alt="Generated result"
            className="max-h-96 w-full rounded-lg border object-contain"
          />
        )}
        {result.audioUrl && (
          <audio controls src={result.audioUrl} className="w-full">
            <a href={result.audioUrl}>Download audio</a>
          </audio>
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
            {item.kind === "image" && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={item.url}
                alt="Generated result"
                className="max-h-96 w-full rounded-lg border object-contain"
              />
            )}
            {item.kind === "audio" && (
              <audio
                aria-label={item.label}
                controls
                src={item.url}
                className="w-full"
              />
            )}
            {item.kind === "video" && (
              <video
                aria-label={item.label}
                controls
                src={item.url}
                className="max-h-96 w-full rounded-lg"
              />
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
        {result.job && (
          <div className="space-y-2">
            <p className="text-sm">Job status: {result.job.status}</p>
            {jobError && <ErrorState message={jobError} onRetry={onCheckJob} />}
            {result.job.error && (
              <p className="text-destructive text-sm">{result.job.error}</p>
            )}
            {!result.media?.length &&
              !["failed", "cancelled", "canceled"].includes(
                result.job.status,
              ) && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={checkingJob}
                  onClick={onCheckJob}
                >
                  {checkingJob ? "Checking job…" : "Check job"}
                </Button>
              )}
            {!result.media?.length &&
              ![
                "succeeded",
                "completed",
                "failed",
                "cancelled",
                "canceled",
              ].includes(result.job.status) && (
                <p className="text-muted-foreground text-sm">
                  The gateway is still processing this request. Check again to
                  load its output.
                </p>
              )}
          </div>
        )}
        {!result.imageUrl &&
          !result.audioUrl &&
          !result.media?.length &&
          !result.job &&
          !result.vector &&
          !result.probabilities && (
            <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-sm whitespace-pre-wrap">
              {result.body}
            </pre>
          )}
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <Fact
          label={result.job ? "Request time" : "Time"}
          value={`${result.elapsedMs} ms`}
        />
        {result.cost && <Fact label="Cost" value={result.cost} />}
        <Fact
          label="Receipt"
          value={
            result.receiptId ? (
              <ReceiptLink id={result.receiptId} />
            ) : (
              "Not available for this session"
            )
          }
        />
      </dl>
      {result.technical && (
        <details className="border-t pt-3">
          <summary className="min-h-11 cursor-pointer text-sm font-medium">
            Technical details
          </summary>
          <pre
            tabIndex={0}
            className="bg-muted max-h-80 overflow-auto rounded-lg p-3 font-mono text-xs break-all whitespace-pre-wrap"
          >
            {result.technical}
          </pre>
        </details>
      )}
      <details className="space-y-2 border-t pt-3">
        <summary className="flex min-h-11 cursor-pointer items-center gap-2 text-sm font-medium">
          <Copy aria-hidden="true" className="size-4" />
          Copy as
        </summary>
        <SnippetBlock snippets={codeSnippets(result.request)} />
      </details>
    </div>
  );
}

function WorkflowFileInputs({
  workflow,
  files,
  onChange,
}: {
  workflow: WorkflowOption;
  files: Record<string, File | null>;
  onChange: (field: string, file: File | null) => void;
}) {
  const fileSpecs = workflowFileSpecs(workflow);
  if (!fileSpecs.length) return null;
  return (
    <div className="space-y-3">
      {fileSpecs.map((fileSpec) => (
        <label key={fileSpec.field} className="block space-y-2">
          <span className="text-sm font-medium">
            {fileSpec.field}
            {fileSpec.required !== false && (
              <span className="text-destructive ml-1">*</span>
            )}
          </span>
          <Input
            type="file"
            accept={fileSpec.type}
            onChange={(event) =>
              onChange(fileSpec.field, event.target.files?.[0] ?? null)
            }
          />
          <span className="text-muted-foreground text-xs">
            {files[fileSpec.field]?.name ??
              fileSpec.placeholder ??
              "Choose a file for this workflow input."}
          </span>
        </label>
      ))}
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
  const [copied, setCopied] = useState<string | null>(null);
  async function copy(language: string, code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(language);
    } catch {
      setCopied("Copy failed");
    }
  }
  return (
    <Tabs
      value={activeLanguage || firstLanguage}
      onValueChange={(language) => {
        setActiveLanguage(language);
        setCopied(null);
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
              snippets.find((item) => item.language === activeLanguage) ??
              snippets[0];
            if (snippet) void copy(snippet.language, snippet.code);
          }}
        >
          {copied === "Copy failed"
            ? "Copy failed"
            : copied
              ? "Copied"
              : "Copy"}
        </Button>
      </div>
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
  value,
  fallbackLabel,
  options,
  onChange,
}: {
  value: string;
  fallbackLabel: string;
  options: CatalogOption[];
  onChange: (value: string) => void;
}) {
  const values = [
    ...((value || fallbackLabel) &&
    !options.some((option) => option.id === value)
      ? [
          {
            id: value || fallbackLabel,
            label: `${value || fallbackLabel} · Unverified model`,
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
  onRetry,
}: {
  state: CatalogState;
  section: CatalogSection;
  onRetry: () => void;
}) {
  const label = section === "workflows" ? "Workflow" : "Model";
  if (state === "loading")
    return (
      <Badge variant="outline">Loading {label.toLowerCase()} catalog</Badge>
    );
  // ErrorState action buttons must not submit the surrounding Run form.
  if (state === "error")
    return (
      <div onClick={(event) => event.preventDefault()}>
        <ErrorState
          heading={`${label} catalog unavailable`}
          message={`${label} catalog could not be loaded.`}
          action={{
            label: `Retry ${label.toLowerCase()} catalog`,
            onClick: onRetry,
          }}
        />
      </div>
    );
  if (state === "empty")
    return <Badge variant="outline">No {section} are listed</Badge>;
  return null;
}
