"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  AudioLines,
  Bot,
  Copy,
  FileAudio,
  Image as ImageIcon,
  KeyRound,
  Loader2,
  Mic,
  Pause,
  Play,
  Sigma,
  Sparkles,
} from "lucide-react";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import {
  PageHeader,
  EmptyState,
  ErrorState,
  SecretReveal,
} from "@/components/product";

type TryTab = "chat" | "image" | "speech" | "transcribe" | "decide" | "embed";
type TryKey = { secret: string; appName: string };
type RequestInfo = { path: string; body: unknown; multipart?: boolean };
type CatalogOption = {
  id: string;
  label: string;
  capability: TryTab;
  price: string;
  locality: string;
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
};

const TAB_ITEMS: Array<{
  value: TryTab;
  label: string;
  icon: typeof Bot;
  help: string;
}> = [
  {
    value: "chat",
    label: "Chat",
    icon: Bot,
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
    icon: FileAudio,
    help: "Upload or record audio and get text.",
  },
  {
    value: "decide",
    label: "Decide",
    icon: Sparkles,
    help: "Ask System One to choose from options.",
  },
  {
    value: "embed",
    label: "Embed",
    icon: Sigma,
    help: "Create a vector and inspect its shape.",
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
  const url = request.path.startsWith("/v1/")
    ? `${typeof window === "undefined" ? "" : window.location.origin}${request.path}`
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
  if (cost.kind === "free") return "Free";
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

export default function TryPage() {
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
      if (catalogResponse.ok) {
        const grouped = groupCatalog(await catalogResponse.json());
        setCatalog(grouped);
        setModel((current) => {
          const next = { ...current };
          for (const tab of TAB_ITEMS) {
            const options = grouped[tab.value] ?? [];
            if (options.length) next[tab.value] = options[0].id;
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
    const request = buildRequest(tab);
    try {
      let result: Awaited<ReturnType<typeof publicRequest>>;
      if (request.multipart) {
        if (!file) throw new Error("Choose an audio file or record one first.");
        const form = new FormData();
        form.set("model", model.transcribe);
        form.set("file", file);
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
                  className="bg-card space-y-4 rounded-xl border p-5"
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
                      Aliases follow your gateway’s model choices. This Try key
                      may still be denied by its access plan.
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
                  {tab.value !== "transcribe" && (
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
          Copy as
        </h3>
        <SnippetBlock snippets={codeSnippets(result.request)} />
      </div>
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
    <select
      aria-label="Model"
      className="border-input bg-background h-11 w-full rounded-md border px-3 text-sm"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {values.map((option) => (
        <option key={option.id} value={option.id}>
          {option.label} · {option.locality} · {option.price}
        </option>
      ))}
    </select>
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
