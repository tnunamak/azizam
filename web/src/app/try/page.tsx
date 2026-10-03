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
  KeyRound,
  Loader2,
  Mic,
  Pause,
  Play,
  Layers,
} from "lucide-react";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
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
import {
  PageHeader,
  EmptyState,
  ErrorState,
  SecretReveal,
} from "@/components/product";

type TryTab =
  | "chat"
  | "image"
  | "speech"
  | "transcribe"
  | "decide"
  | "embed"
  | "workflow";
type TryKey = { secret: string; appName: string };
type RequestInfo = {
  path: string;
  body: unknown;
  multipart?: boolean;
  form?: Record<string, unknown>;
  files?: WorkflowExampleFile[];
  snippets?: Array<{ language: string; code: string }>;
};
type WorkflowExampleFile = {
  field: string;
  placeholder: string;
  type?: string;
  required?: boolean;
};
type CatalogOption = {
  id: string;
  label: string;
  capability: TryTab;
  price: string;
  locality: string;
  workflow?: WorkflowOption;
};
type WorkflowInput = {
  name: string;
  type: string;
  request_field?: string;
  required?: boolean;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  accepted?: boolean;
  ignored?: boolean;
};
type WorkflowOption = {
  id: string;
  short_id?: string;
  title: string;
  description?: string;
  inputs: WorkflowInput[];
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
type Result = {
  title: string;
  body: string;
  elapsedMs: number;
  cost: string;
  receiptId: string | null;
  request: RequestInfo;
  imageUrl?: string;
  audioUrl?: string;
  downloadUrl?: string;
  downloadName?: string;
  vector?: { dimensions: number; min: number; max: number; mean: number };
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
    help: "Send one message and read one answer.",
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
    label: "Transcribe",
    icon: Mic,
    help: "Upload or record audio and get text.",
  },
  {
    value: "decide",
    label: "Decide",
    icon: GitBranch,
    help: "Ask System One to choose from options.",
  },
  {
    value: "embed",
    label: "Embed",
    icon: Layers,
    help: "Create a vector and inspect its shape.",
  },
  {
    value: "workflow",
    label: "Workflow",
    icon: GitBranch,
    help: "Run a manifest workflow by id.",
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

function baseUrl() {
  if (typeof window === "undefined") return "";
  return `${window.location.origin}/openai/v1`;
}

function publicPath(path: string) {
  if (path.startsWith("/gateway/")) return path;
  return path.startsWith("/v1/") ? path : `/openai/v1${path}`;
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
    throw new ApiError("public_request_failed", message, response.status);
  }
  return { response, payload, elapsedMs };
}

function receiptFrom(response: Response, payload: unknown) {
  const header =
    response.headers.get("x-ai-gateway-receipt-id") ??
    response.headers.get("x-ai-gateway-request-id") ??
    response.headers.get("x-request-id");
  if (header) return header;
  if (typeof payload === "object" && payload !== null) {
    const requestId = (payload as Record<string, unknown>).request_id;
    if (typeof requestId === "string") return requestId;
  }
  return null;
}

async function verifiedReceiptId(response: Response, payload: unknown) {
  const requestId = receiptFrom(response, payload);
  if (!requestId) return null;
  try {
    const receipt = await fetch(
      `/me/api/receipts/${encodeURIComponent(requestId)}`,
      {
        credentials: "same-origin",
      },
    );
    return receipt.ok ? requestId : null;
  } catch {
    return null;
  }
}

function costFrom(payload: unknown, response?: Response) {
  const reported = response?.headers.get("x-litellm-response-cost");
  if (reported !== null && reported !== undefined && reported.trim()) {
    const amount = Number(reported);
    if (Number.isFinite(amount) && amount >= 0)
      return `$${amount.toFixed(amount < 0.01 && amount > 0 ? 6 : 2)}`;
  }
  if (typeof payload !== "object" || payload === null)
    return "Not measured yet";
  const object = payload as Record<string, unknown>;
  const cost = object.cost ?? object.remote_cost ?? object.measured_cost;
  if (typeof cost === "object" && cost !== null) {
    const amount = (cost as { amount_usd?: unknown }).amount_usd;
    if (typeof amount === "number")
      return amount === 0 ? "$0.00" : `$${amount}`;
  }
  if (typeof cost === "number") return cost === 0 ? "$0.00" : `$${cost}`;
  if (typeof cost === "string" && cost.trim()) return cost;
  return "Not measured yet";
}

function codeSnippets(request: RequestInfo) {
  if (request.snippets?.length) return request.snippets;
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const url =
    request.path.startsWith("/v1/") || request.path.startsWith("/gateway/")
      ? `${origin}${request.path}`
      : `${baseUrl()}${request.path}`;
  const model = (request.body as { model?: string }).model ?? "";
  const jsonBody = JSON.stringify(request.body, null, 2);
  if (request.multipart) {
    return [
      {
        language: "curl",
        code: `curl '${url}' \\\n  -H "Authorization: Bearer $GATEWAY_API_KEY" \\\n  -F model='${model}' \\\n  -F file='@sample.wav'`,
      },
      {
        language: "Python",
        code: `import os\nfrom openai import OpenAI\n\nclient = OpenAI(base_url="${baseUrl()}", api_key=os.environ["GATEWAY_API_KEY"])\nwith open("sample.wav", "rb") as audio:\n    print(client.audio.transcriptions.create(model="${model}", file=audio))`,
      },
      {
        language: "JavaScript",
        code: `import fs from "node:fs";\nimport OpenAI from "openai";\n\nconst client = new OpenAI({ baseURL: "${baseUrl()}", apiKey: process.env.GATEWAY_API_KEY });\nconsole.log(await client.audio.transcriptions.create({ model: "${model}", file: fs.createReadStream("sample.wav") }));`,
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
        capability,
        price: costLabel(record.cost),
        locality: localityLabel(record),
      };
      if (!(grouped[capability] ?? []).some((existing) => existing.id === id)) {
        grouped[capability] = [...(grouped[capability] ?? []), option];
      }
    }
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
        inputs: Array.isArray(record.inputs)
          ? (record.inputs as WorkflowInput[])
          : [],
        engine_binding: stringValue(record.engine_binding) ?? undefined,
        native_route:
          record.native_route === null
            ? null
            : stringValue(record.native_route) ?? undefined,
        example_request: workflowExampleRequest(record.example_request),
      };
      if (!workflow.id) continue;
      options.push({
        id: workflow.id,
        label: workflow.title || workflow.id,
        capability: "workflow" as const,
        price: "Cost not measured yet",
        locality: "Workflow manifest",
        workflow,
      });
  }
  return options;
}

function workflowExampleRequest(value: unknown): WorkflowOption["example_request"] {
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

function workflowFileSpecs(workflow: WorkflowOption) {
  return (workflow.example_request?.files ?? []).map((file) => {
    const input = workflow.inputs.find(
      (item) =>
        item.name === file.field ||
        item.request_field === file.field,
    );
    return {
      ...file,
      required: input ? input.required !== false : true,
    };
  });
}

function workflowSnippets(workflow: WorkflowOption) {
  const snippets = [
    { language: "curl", code: workflow.example_request?.curl },
    { language: "Python", code: workflow.example_request?.python },
    { language: "JavaScript", code: workflow.example_request?.javascript },
  ].filter(
    (snippet): snippet is { language: string; code: string } =>
      typeof snippet.code === "string" && snippet.code.trim().length > 0,
  );
  return snippets.length ? snippets : undefined;
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
    const tab = INTENT_TABS[intent];
    if (tab) tabs.add(tab);
  }
  return [...tabs];
}

function costLabel(value: unknown) {
  if (typeof value !== "object" || value === null)
    return "Cost not measured yet";
  const cost = value as { kind?: unknown; price?: unknown };
  if (cost.kind === "free") return "No provider charge";
  if (cost.kind === "paid") {
    return cost.price == null
      ? "Paid · Cost not measured yet"
      : `Paid · ${String(cost.price)}`;
  }
  return "Cost not measured yet";
}

function localityLabel(record: Record<string, unknown>) {
  if (record.locality === "local") return "Local";
  if (record.locality === "remote") {
    const provider = stringValue(record.provider);
    return provider ? `Remote · ${provider}` : "Remote";
  }
  return "Locality not measured yet";
}

function workflowExampleBody(workflow: WorkflowOption) {
  return requestIsMultipart(workflow)
    ? workflow.example_request?.form ?? {}
    : workflow.example_request?.body ?? {};
}

function requestIsMultipart(workflow: WorkflowOption) {
  return workflow.example_request?.content_type?.includes(
    "multipart/form-data",
  );
}

export default function TryPage() {
  const searchParams = useSearchParams();
  const requestedWorkflow = searchParams.get("workflow");
  const [key, setKey] = useState<TryKey | null>(null);
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState(false);
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
  const [workflowFiles, setWorkflowFiles] = useState<Record<string, File | null>>(
    {},
  );
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const objectUrls = useRef<string[]>([]);
  const mounted = useRef(true);
  const activeTabRef = useRef<TryTab>("chat");
  const chunks = useRef<Blob[]>([]);
  const [running, setRunning] = useState<TryTab | null>(null);
  const [results, setResults] = useState<Partial<Record<TryTab, Result>>>({});
  const [errors, setErrors] = useState<Partial<Record<TryTab, string>>>({});
  const [catalog, setCatalog] = useState<
    Partial<Record<TryTab, CatalogOption[]>>
  >({});
  const [catalogState, setCatalogState] = useState<
    "idle" | "loading" | "ready" | "empty" | "error"
  >("idle");
  const parsedOptions = useMemo(
    () =>
      options
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .slice(0, 3),
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
    if (requestedWorkflow) {
      setActiveTab("workflow");
      setModel((current) => ({ ...current, workflow: requestedWorkflow }));
    }
  }, [requestedWorkflow]);

  useEffect(() => {
    if (!selectedWorkflow) return;
    const body = workflowExampleBody(selectedWorkflow);
    const inputs = body && typeof body === "object" ? { ...body } : {};
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
      setKey({ secret: minted.api_key, appName: app.name });
      setSavedKey(false);
      void loadCatalog(minted.api_key);
    } catch (error) {
      setKeyError(
        error instanceof Error
          ? error.message
          : "Could not prepare a Try key. Try again.",
      );
    } finally {
      setKeyBusy(false);
    }
  }

  async function startRecording() {
    setKeyError(null);
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
      setKeyError("Recording is not available in this browser.");
    }
  }

  function stopRecording() {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  async function loadCatalog(secret: string) {
    setCatalogState("loading");
    try {
      const catalogResponse = await fetch("/me/api/models/catalog", {
        credentials: "same-origin",
      });
      const workflowsResponse = await fetch("/me/api/workflows", {
        credentials: "same-origin",
      });
      const workflowOptions = workflowsResponse.ok
        ? groupWorkflows(await workflowsResponse.json())
        : [];
      if (catalogResponse.ok) {
        const grouped = groupCatalog(await catalogResponse.json());
        if (workflowOptions.length) grouped.workflow = workflowOptions;
        setCatalog(grouped);
        setModel((current) => {
          const next = { ...current };
          for (const tab of TAB_ITEMS) {
            const options = grouped[tab.value] ?? [];
            if (options.length) next[tab.value] = options[0].id;
          }
          if (
            requestedWorkflow &&
            workflowOptions.some((option) => option.id === requestedWorkflow)
          ) {
            next.workflow = requestedWorkflow;
          }
          return next;
        });
        setCatalogState(Object.keys(grouped).length ? "ready" : "empty");
        return;
      }
      const response = await fetch("/openai/v1/models", {
        headers: { authorization: `Bearer ${secret}` },
      });
      if (!response.ok) {
        setCatalogState(catalogResponse.status === 404 ? "empty" : "error");
        return;
      }
      const payload = (await response.json()) as {
        data?: Array<{ id?: string }>;
      };
      const ids = payload.data?.map((item) => item.id).filter(Boolean) as
        | string[]
        | undefined;
      setCatalog({
        chat: (ids ?? []).map((id) => ({
          id,
          label: id,
          capability: "chat",
          price: "Cost not measured yet",
          locality: "Available to this Try key",
        })),
      });
      setCatalogState(ids?.length ? "ready" : "empty");
    } catch {
      setCatalogState("error");
    }
  }

  function buildRequest(tab: TryTab): RequestInfo {
    if (tab === "chat") {
      return {
        path: "/chat/completions",
        body: {
          model: model.chat,
          messages: [{ role: "user", content: prompt.chat }],
        },
      };
    }
    if (tab === "image") {
      return {
        path: "/images/generations",
        body: { model: model.image, prompt: prompt.image, size: "1024x1024" },
      };
    }
    if (tab === "speech") {
      return {
        path: "/audio/speech",
        body: { model: model.speech, voice, input: prompt.speech },
      };
    }
    if (tab === "transcribe") {
      return {
        path: "/audio/transcriptions",
        body: { model: model.transcribe },
        multipart: true,
      };
    }
    if (tab === "decide") {
      return {
        path: "/v1/systemone",
        body: {
          model: model.decide,
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
      let body: Record<string, unknown> = selectedWorkflow.example_request?.body
        ? { ...selectedWorkflow.example_request.body }
        : {};
      let formBody: Record<string, unknown> = selectedWorkflow.example_request?.form
        ? { ...selectedWorkflow.example_request.form }
        : {};
      try {
        const parsed = JSON.parse(prompt.workflow || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          if (multipart) {
            formBody = { ...formBody, ...(parsed as Record<string, unknown>) };
          } else {
            body = { ...body, ...(parsed as Record<string, unknown>) };
          }
        }
      } catch {
        throw new Error("Workflow inputs must be a JSON object.");
      }
      if (!multipart && body.model === undefined) {
        body.model = selectedWorkflow.short_id ?? selectedWorkflow.id;
      }
      return {
        path: route,
        body,
        form: formBody,
        files: workflowFileSpecs(selectedWorkflow),
        multipart,
        snippets: workflowSnippets(selectedWorkflow),
      };
    }
    return {
      path: "/embeddings",
      body: { model: model.embed, input: prompt.embed },
    };
  }

  async function run(tab: TryTab, event: FormEvent) {
    event.preventDefault();
    if (!key?.secret) return;
    setRunning(tab);
    setErrors((current) => ({ ...current, [tab]: undefined }));
    try {
      const request = buildRequest(tab);
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
          for (const [name, value] of Object.entries(
            request.form ?? {},
          )) {
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
          form.set("model", model.transcribe);
          form.set("file", file);
        }
        result = await publicRequest(request.path, key.secret, {
          method: "POST",
          body: form,
        });
      } else {
        result = await publicRequest(request.path, key.secret, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(request.body),
        });
      }
      const receiptId = await verifiedReceiptId(
        result.response,
        result.payload,
      );
      setResults((current) => ({
        ...current,
        [tab]: formatResult(
          tab,
          result.payload,
          result.elapsedMs,
          costFrom(result.payload, result.response),
          receiptId,
          request,
        ),
      }));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [tab]:
          error instanceof ApiError && tab === "decide" && error.status === 404
            ? "Decisions not enabled on this gateway"
            : error instanceof Error
              ? error.message
              : "This request could not be completed.",
      }));
    } finally {
      setRunning(null);
    }
  }

  function formatResult(
    tab: TryTab,
    payload: unknown,
    elapsedMs: number,
    cost: string,
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
        objectUrls.current.forEach((url) => URL.revokeObjectURL(url));
        objectUrls.current = [audioUrl];
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
      let imageUrl: string | undefined;
      let audioUrl: string | undefined;
      let downloadUrl: string | undefined;
      if (payload instanceof Blob) {
        const objectUrl = URL.createObjectURL(payload);
        objectUrls.current.push(objectUrl);
        if (payload.type.startsWith("image/")) {
          imageUrl = objectUrl;
        } else if (payload.type.startsWith("audio/")) {
          audioUrl = objectUrl;
        } else {
          downloadUrl = objectUrl;
        }
      }
      return {
        title: "Workflow result",
        body:
          payload instanceof Blob
            ? "Workflow returned media bytes."
            : JSON.stringify(payload, null, 2),
        imageUrl,
        audioUrl,
        downloadUrl,
        downloadName: "workflow-result",
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
      <PageHeader
        title="Try"
        answer="Does it work, and what does the request look like?"
        actions={
          key ? <Badge variant="outline">Session key ready</Badge> : undefined
        }
      />

      {!key ? (
        <section className="bg-card rounded-xl border p-5">
          {keyBusy ? (
            <div
              aria-busy="true"
              aria-label="Preparing Try key"
              className="space-y-3"
            >
              <Skeleton className="h-6 w-48" />
              <Skeleton className="h-20 w-full" />
            </div>
          ) : keyError ? (
            <ErrorState message={keyError} onRetry={() => void prepareKey()} />
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="max-w-prose space-y-1">
                <h2 className="font-semibold">Prepare a session key</h2>
                <p className="text-muted-foreground text-sm">
                  Try calls the public API with your own key. Create a Try app
                  key before running requests.
                </p>
              </div>
              <Button variant="outline" onClick={() => void prepareKey()}>
                <KeyRound aria-hidden="true" />
                Prepare Try key
              </Button>
            </div>
          )}
        </section>
      ) : (
        <section className="bg-card rounded-xl border p-5">
          {!savedKey ? (
            <div className="space-y-4">
              <div>
                <h2 className="font-semibold">Try app key</h2>
                <p className="text-muted-foreground mt-1 text-sm">
                  This key stays only in this browser session. Save it if you
                  want to reuse it outside Try.
                </p>
              </div>
              <SecretReveal
                secret={key.secret}
                onSaved={() => setSavedKey(true)}
              />
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-semibold">
                  {key.appName} key saved for this session
                </h2>
                <p className="text-muted-foreground mt-1 text-sm">
                  Requests below use public API routes. Permission errors are
                  shown from the gateway.
                </p>
              </div>
              <Button variant="outline" onClick={() => setKey(null)}>
                Clear session key
              </Button>
            </div>
          )}
        </section>
      )}

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
                  onSubmit={(event) => void run(tab.value, event)}
                  className="bg-card min-w-0 space-y-4 rounded-xl border p-5"
                >
                  <div>
                    <h2 className="flex items-center gap-2 font-semibold">
                      <tab.icon aria-hidden="true" className="size-4" />
                      {tab.label}
                    </h2>
                    <p className="text-muted-foreground mt-1 text-sm">
                      {tab.help}
                    </p>
                  </div>
                  <label className="block space-y-2">
                    <span className="text-sm font-medium">Model</span>
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
                    <span className="text-muted-foreground text-xs">
                      {tab.value === "workflow"
                        ? "Workflow choices come from your access plan."
                        : "Aliases follow your gateway’s model choices. This Try key may still be denied by its access plan."}
                    </span>
                  </label>
                  {tab.value === activeTab && (
                    <CatalogNotice state={catalogState} />
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
                    <WorkflowInputs workflow={selectedWorkflow} />
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
                  {tab.value !== "transcribe" && (
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">
                        {tab.value === "decide"
                          ? "Question"
                          : tab.value === "workflow"
                            ? "Inputs JSON"
                            : "Prompt"}
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
                    <label className="block space-y-2">
                      <span className="text-sm font-medium">Voice</span>
                      <Input
                        value={voice}
                        onChange={(event) => setVoice(event.target.value)}
                      />
                    </label>
                  )}
                  {tab.value === "transcribe" && (
                    <div className="space-y-3">
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
                        One option per line. The first three are sent.
                      </span>
                    </label>
                  )}
                  <Button
                    type="submit"
                    variant="outline"
                    disabled={!key?.secret || running !== null}
                  >
                    {running === tab.value ? (
                      <Loader2 aria-hidden="true" className="animate-spin" />
                    ) : (
                      <Play aria-hidden="true" />
                    )}
                    Run
                  </Button>
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
                    <ErrorState message={currentError} />
                  ) : currentResult ? (
                    <ResultView result={currentResult} />
                  ) : (
                    <EmptyState
                      icon={tab.icon}
                      title={`Run ${tab.label.toLowerCase()}`}
                      description="The last result for this tab will appear here with timing, cost and code."
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

function ResultView({ result }: { result: Result }) {
  return (
    <div className="min-w-0 space-y-5">
      <div className="space-y-2">
        <h2 className="font-semibold">{result.title}</h2>
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
        {result.probabilities && (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left">
                  <th className="p-3 font-medium">Option</th>
                  <th className="p-3 font-medium">Probability</th>
                </tr>
              </thead>
              <tbody>
                {result.probabilities.map((row) => (
                  <tr key={row.option} className="border-b last:border-0">
                    <td className="p-3">{row.option}</td>
                    <td className="p-3">
                      {row.probability === null
                        ? "Not measured yet"
                        : `${Math.round(row.probability * 100)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!result.imageUrl &&
          !result.audioUrl &&
          !result.vector &&
          !result.probabilities && (
            <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-sm whitespace-pre-wrap">
              {result.body}
            </pre>
          )}
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <Fact label="Time" value={`${result.elapsedMs} ms`} />
        <Fact label="Cost" value={result.cost} />
        <Fact
          label="Receipt"
          value={
            result.receiptId ? (
              <a
                className="underline underline-offset-4"
                href={`/r/${result.receiptId}`}
              >
                {result.receiptId}
              </a>
            ) : (
              "Not measured yet"
            )
          }
        />
      </dl>
      <div className="space-y-2">
        <h3 className="flex items-center gap-2 text-sm font-medium">
          <Copy aria-hidden="true" className="size-4" />
          {result.request.snippets?.length ? "Example request" : "Copy as"}
        </h3>
        <SnippetBlock snippets={codeSnippets(result.request)} />
      </div>
    </div>
  );
}

function WorkflowInputs({ workflow }: { workflow: WorkflowOption }) {
  const rows = workflow.inputs ?? [];
  if (!rows.length) return null;
  return (
    <div className="rounded-lg border">
      <div className="border-b p-3">
        <h3 className="text-sm font-medium">Typed inputs</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground border-b text-left">
              <th className="p-3 font-medium">Name</th>
              <th className="p-3 font-medium">Type</th>
              <th className="p-3 font-medium">Default</th>
              <th className="p-3 font-medium">Applies</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((input) => (
              <tr key={input.name} className="border-b last:border-0">
                <td className="p-3 font-mono text-xs">
                  {input.name}
                  {input.required && (
                    <span className="text-destructive ml-1">*</span>
                  )}
                </td>
                <td className="p-3">{input.type}</td>
                <td className="p-3">{valueText(input.default)}</td>
                <td className="p-3">
                  {input.ignored ? "Ignored" : input.accepted === false ? "Unavailable" : "Accepted"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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

function valueText(value: unknown) {
  if (value === undefined || value === null || value === "") return "None";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
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
    await navigator.clipboard.writeText(code);
    setCopied(language);
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
          className="self-end sm:self-auto"
          onClick={() => {
            const snippet =
              snippets.find((item) => item.language === activeLanguage) ??
              snippets[0];
            if (snippet) void copy(snippet.language, snippet.code);
          }}
        >
          {copied ? "Copied" : "Copy"}
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
    ...(!options.some((option) => option.id === value)
      ? [
          {
            id: value || fallbackLabel,
            label: `${value || fallbackLabel} · Unverified model`,
            price: "Cost not measured yet",
            locality: "Not in the catalog",
          },
        ]
      : []),
    ...options,
  ];
  return (
    <Select
      value={value}
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
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {values.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            {option.label} · {option.locality} · {option.price}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function CatalogNotice({
  state,
}: {
  state: "idle" | "loading" | "ready" | "empty" | "error";
}) {
  if (state === "loading") {
    return <Badge variant="outline">Loading model catalog</Badge>;
  }
  if (state === "ready") {
    return <Badge variant="outline">Catalog loaded</Badge>;
  }
  if (state === "error") {
    return <Badge variant="outline">Catalog could not be loaded</Badge>;
  }
  if (state === "empty") {
    return <Badge variant="outline">No catalog rows yet</Badge>;
  }
  return <Badge variant="outline">Cost not measured yet</Badge>;
}
