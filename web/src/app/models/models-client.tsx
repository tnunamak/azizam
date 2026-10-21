"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Check,
  CircleHelp,
  CircleSlash,
  Clock3,
  Copy,
  Loader2,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { productApi, type CatalogRow, type ModelTag } from "@/lib/product-api";
import { accountApi } from "@/lib/account-api";
import { capabilityLabels as terms, preferredTerms, taskNames } from "@/lib/terms";
import { availabilityReason } from "@/lib/status-copy";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { copyText } from "@/components/ui/copy-button";
import { useViewer } from "@/components/shell/session";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  CodeBlock,
  EmptyState,
  ErrorState,
  PageHeader,
  StatusPill,
} from "@/components/product";

/**
 * The Models page answers two questions on one list:
 * - a builder's "which model name do I put in my app for this task?", and
 * - the owner's "what is on this gateway, what does it cost, is it ready?".
 *
 * Every model name is one row, grouped under its task. Nothing hides behind
 * "N more" or a collapsed "All models": OpenRouter, Vercel's AI Gateway and
 * OpenAI's model pages all show the full list and let search narrow it.
 */

type AvailabilityState =
  | "ready"
  | "starting"
  | "busy"
  | "degraded"
  | "idle"
  | "idle-startable"
  | "standby_stopped"
  | "unknown"
  | "unavailable"
  | "not_measured";

type Task =
  | "chat"
  | "image"
  | "video"
  | "music"
  | "sfx"
  | "speech"
  | "audio"
  | "transcribe"
  | "decide"
  | "embed"
  | "rerank";

const taskOrder: Task[] = [
  "chat",
  "image",
  "video",
  "music",
  "sfx",
  "speech",
  "audio",
  "transcribe",
  "decide",
  "embed",
  "rerank",
];

/**
 * Section names: the capability names (lib/terms.ts). Decisions and
 * embeddings use the plain task names Try uses, which the sweep requires on
 * member screens; the sentence under each name says what it is for.
 */
const taskLabels: Record<Task, string> = {
  chat: terms.chat,
  image: terms.image,
  video: terms.video,
  music: terms.music,
  sfx: terms.sound_effects,
  speech: terms.speech,
  audio: terms.audio,
  transcribe: terms.transcribe,
  decide: taskNames.decisions,
  embed: taskNames.embeddings,
  rerank: terms.reranking,
};

/** One plain sentence per task: what an app uses it for. */
const taskExplainers: Record<Task, string> = {
  chat: "Answers, writing and code, for chat apps and agents.",
  image: "Make or edit a picture from a description.",
  video: "Make a short video from a description or a picture.",
  music: "Songs and instrumentals from a description.",
  sfx: "Short sounds from a description.",
  speech: "Read text aloud.",
  audio: "Music and sounds from a description.",
  transcribe: "Turn a recording into text.",
  decide: "Pick one answer from options you give, for routing and yes-or-no checks.",
  embed: "Vectors that let apps search text and find similar passages.",
  rerank: "Sort search results by how well they match.",
};

const tagLabels: Record<ModelTag, string> = {
  reasoning: "Thinks through questions",
  "tool-use": "Uses tools",
  vision: "Understands images",
  "file-input": "Reads files",
};

// The server classifies each row from its /v1/models type and modalities.
function rowTask(row: CatalogRow): Task {
  const task = (row.capability ?? row.capabilities[0]?.capability ?? "chat") as Task;
  return taskOrder.includes(task) ? task : "chat";
}

function taskLabel(row: CatalogRow): string {
  return taskLabels[rowTask(row)];
}

function letters(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * The model's own name when it adds something to the model name: "Supertonic"
 * for `tts`, but nothing for `coder-q5` ("Coder Q5") or a title that only
 * names the task ("Speech" for `tts`).
 */
export function modelTitle(row: CatalogRow): string | undefined {
  const id = letters(row.id);
  const task = letters(taskLabel(row));
  const serverTask = letters(row.capability_title ?? "");
  // "Embeddings · Embed" only names the task twice; "embed" is the task too.
  const plain = (word: string) => (word.startsWith("embed") ? "embed" : word);
  const known = new Set([id, task, serverTask, letters(row.capability ?? "")].map(plain));
  for (const candidate of [row.title ?? row.display_name, row.recommended_model]) {
    if (!candidate) continue;
    const parts = candidate.split("·").map((part) => plain(letters(part))).filter(Boolean);
    if (!parts.length || parts.every((part) => known.has(part))) continue;
    return candidate;
  }
  return undefined;
}

function isLocalFree(row: CatalogRow): boolean {
  return row.locality === "local" && row.cost.kind !== "paid";
}

type CostKind = "none" | "paid" | "unknown";

function costKind(row: CatalogRow): CostKind {
  if (row.cost.kind === "paid") return "paid";
  if (isLocalFree(row) || row.cost.kind === "free") return "none";
  return "unknown";
}

/** VOICE.md money words; a cost cell is never blank (ban 26). */
export function costLabel(row: CatalogRow): string {
  if (row.cost.kind === "paid") {
    const price = row.cost.price;
    if (typeof price === "string" && price.trim()) return price;
    if (typeof price === "number" && Number.isFinite(price)) return `Paid · ${price}`;
    return "Paid · price not recorded";
  }
  if (isLocalFree(row)) return preferredTerms.localPricing;
  if (row.cost.kind === "free") return "No provider charge";
  return "Cost not recorded";
}

function providerLabel(row: CatalogRow): string | undefined {
  // A local row's provider field is a service id, not a name.
  if (row.locality !== "remote") return undefined;
  return row.engine_label ?? row.provider ?? undefined;
}

function engineLabel(row: CatalogRow): string {
  return (
    row.engine_label ??
    (row.locality === "remote" ? row.provider : undefined) ??
    "Engine or provider not specified"
  );
}

function localityLabel(row: CatalogRow): string | undefined {
  if (row.locality === "local") return "On your equipment";
  if (row.locality === "remote") return providerLabel(row) ?? "Online provider";
  return undefined;
}

function timingLabel(row: CatalogRow): string | undefined {
  const value = row.timing.typical_seconds;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return undefined;
  }
  const duration =
    value < 1
      ? `About ${Math.round(value * 1000)} ms`
      : `About ${value.toFixed(value >= 10 ? 0 : 1)} s`;
  if (row.timing.source === "receipts") return `${duration}, measured`;
  if (row.timing.source === "configured") return `${duration}, typical`;
  return duration;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function availabilityState(row: CatalogRow): AvailabilityState {
  if (row.availability.status) {
    const status = row.availability.status.toLowerCase().replaceAll("_", "-");
    if (
      [
        "ready",
        "starting",
        "busy",
        "degraded",
        "idle",
        "idle-startable",
        "unknown",
        "unavailable",
      ].includes(status)
    ) {
      return status as AvailabilityState;
    }
    if (status === "standby-stopped") return "standby_stopped";
    if (status === "not-measured" || status === "not measured yet") {
      return "not_measured";
    }
  }
  if (!row.availability.measured) return "unknown";
  return row.availability.available ? "ready" : "unavailable";
}

const quietStates: Record<
  AvailabilityState,
  { label: string; icon: typeof Check; tone: string }
> = {
  ready: { label: "Ready", icon: Check, tone: "text-success" },
  idle: { label: "Ready to start", icon: Check, tone: "text-success" },
  "idle-startable": { label: "Ready to start", icon: Check, tone: "text-success" },
  standby_stopped: { label: "Ready to start", icon: Check, tone: "text-success" },
  starting: { label: "Starting", icon: Loader2, tone: "text-info" },
  busy: { label: "Busy", icon: Clock3, tone: "text-warning" },
  degraded: { label: "Degraded", icon: CircleSlash, tone: "text-warning" },
  unavailable: { label: "Unavailable", icon: CircleSlash, tone: "text-destructive" },
  unknown: { label: "Not checked yet", icon: CircleHelp, tone: "text-muted-foreground" },
  not_measured: { label: "Not checked yet", icon: CircleHelp, tone: "text-muted-foreground" },
};

function statusWords(row: CatalogRow): { label: string; icon: typeof Check; tone: string } {
  const state = availabilityState(row);
  if (state === "unknown" && row.availability.measured) {
    return { label: "Couldn't check", icon: CircleHelp, tone: "text-muted-foreground" };
  }
  return quietStates[state];
}

/** Quiet status (DESIGN "Status display"): a 14 icon and the word, muted. */
function QuietStatus({ row }: { row: CatalogRow }) {
  const { label, icon: Icon, tone } = statusWords(row);
  return (
    <span className="inline-flex items-center gap-1.5">
      <Icon aria-hidden="true" className={cn("size-3.5 shrink-0", tone)} />
      {label}
    </span>
  );
}

function availabilityAction(
  row: CatalogRow,
  viewerAdmin: boolean,
): { label: string; href: string } | null {
  const configHref = row.locality === "remote" ? "/providers/" : "/engines/";
  if (viewerAdmin) {
    if (row.availability.configured === false) {
      return {
        label: row.locality === "remote" ? "Connect a provider" : "Connect an engine",
        href: configHref,
      };
    }
    if (row.availability.enabled === false) {
      return {
        label: row.locality === "remote" ? "Review provider" : "Review engine",
        href: configHref,
      };
    }
  }
  const state = availabilityState(row);
  if (["unavailable", "unknown", "not_measured"].includes(state)) {
    return { label: "Check status", href: "/status/" };
  }
  return null;
}

function Availability({ row }: { row: CatalogRow }) {
  const state = availabilityState(row);
  if (state === "unknown" || state === "not_measured") {
    return (
      <span className="text-muted-foreground text-meta">
        {statusWords(row).label}
      </span>
    );
  }
  return (
    <StatusPill
      status={state}
      reason={[
        availabilityReason(state, row.availability.reason_code ?? state),
        state === "unavailable" || state === "degraded"
          ? "Choose another model, or check Status for help."
          : null,
      ]
        .filter(Boolean)
        .join(" ")}
    />
  );
}

function itemMatches(row: CatalogRow, query: string): boolean {
  if (!query) return true;
  const haystack = [
    row.id,
    row.name,
    row.display_name,
    row.title,
    row.recommended_model,
    row.description,
    modelDescription(row.description),
    taskLabel(row),
    ...(row.use_names ?? []),
    providerLabel(row),
    ...Object.values(row.resolves_to),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

/** Rewrite only known catalog prose; custom text and identifiers stay intact. */
export function modelDescription(description: string | null | undefined) {
  return description === "Speech synthesis when a backend is configured."
    ? "Read text aloud when a speech engine or provider is connected."
    : description;
}

function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

function exampleBody(row: CatalogRow): Record<string, unknown> {
  return row.examples[0]?.body ?? { model: row.use.model };
}

/** App configuration uses public identifiers only, never catalog examples or keys. */
export function appSnippetsFor(row: CatalogRow, origin: string) {
  // Next replaces this with next.config's basePath in the client build.
  const basePath = process.env.__NEXT_ROUTER_BASEPATH ?? "";
  const baseUrl = `${new URL(origin).origin}${basePath}/openai/v1`;
  const model = JSON.stringify(row.id);
  return [
    {
      language: "curl",
      code: `OPENAI_BASE_URL=${shellSingleQuote(baseUrl)}\nMODEL=${shellSingleQuote(row.id)}\n# Use $MODEL in the model field of your requests.\ncurl "$OPENAI_BASE_URL/models" \\\n  -H "Authorization: Bearer YOUR_KEY"`,
    },
    {
      language: "Python",
      code: `from openai import OpenAI\n\nclient = OpenAI(\n    base_url=${JSON.stringify(baseUrl)},\n    api_key="YOUR_KEY",\n)\nmodel = ${model}\n# Use model in requests for this model\u0027s task.`,
    },
    {
      language: "JavaScript",
      code: `import OpenAI from "openai";\n\nconst client = new OpenAI({\n  baseURL: ${JSON.stringify(baseUrl)},\n  apiKey: "YOUR_KEY",\n});\nconst model = ${model};\n// Use model in requests for this model\u0027s task.`,
    },
  ];
}

export function snippetsFor(row: CatalogRow, origin: string) {
  const path = row.use.path ?? row.use.python?.path ?? row.use.javascript?.path;
  if (!path || !row.examples[0]?.body) return [];
  const url = `${origin}${path}`;
  const body = exampleBody(row);
  const json = JSON.stringify(body);
  const isAudioUpload = /\/audio\/(transcriptions|translations)$/.test(path);
  const isImageUpload = path.endsWith("/images/edits");
  const isMultipart = isAudioUpload || isImageUpload;
  const fileName = isImageUpload ? "sample.png" : "sample.wav";
  const fileField = isImageUpload ? "image" : "file";
  const multipartFields = Object.fromEntries(Object.entries(body)
    .filter(([name]) => name !== fileField)
    .map(([name, value]) => [name, typeof value === "string" ? value : JSON.stringify(value)]));
  const isBinary = path.includes("/audio/speech");
  return [
    {
      language: "curl",
      code: isMultipart
        ? `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AZIZAM_API_KEY" \\\n${Object.entries(multipartFields).map(([name, value]) => `  --form-string ${shellSingleQuote(`${name}=${value}`)}`).join(" \\\n")} \\\n  -F ${fileField}=@${fileName}`
        : `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AZIZAM_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d ${shellSingleQuote(json)}${isBinary ? " \\\n  --output speech.mp3" : ""}`,
    },
    {
      language: "Python",
      code: isMultipart
        ? `import os\nimport json\nimport requests\n\nwith open("${fileName}", "rb") as media:\n    response = requests.post(\n        ${JSON.stringify(url)},\n        headers={"Authorization": f"Bearer {os.environ['AZIZAM_API_KEY']}"},\n        data=json.loads(${JSON.stringify(JSON.stringify(multipartFields))}),\n        files={"${fileField}": media},\n        timeout=120,\n    )\nresponse.raise_for_status()\nprint(response.json())`
        : `import os\nimport json\nimport requests\n\nresponse = requests.post(\n    ${JSON.stringify(url)},\n    headers={\n        "Authorization": f"Bearer {os.environ['AZIZAM_API_KEY']}",\n        "Content-Type": "application/json",\n    },\n    json=json.loads(${JSON.stringify(json)}),\n    timeout=120,\n)\nresponse.raise_for_status()\n${isBinary ? 'open("speech.mp3", "wb").write(response.content)' : "print(response.json())"}`,
    },
    {
      language: "JavaScript",
      code: isMultipart
        ? `import { readFile } from "node:fs/promises";\n\nconst form = new FormData();\nconst fields = ${JSON.stringify(multipartFields)};\nfor (const [name, value] of Object.entries(fields)) form.set(name, String(value));\nform.set("${fileField}", new Blob([await readFile("${fileName}")]), "${fileName}");\n\nconst response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: { Authorization: \`Bearer \${process.env.AZIZAM_API_KEY}\` },\n  body: form,\n});\nif (!response.ok) throw new Error(await response.text());\nconsole.log(await response.json());`
        : `${isBinary ? 'import { writeFile } from "node:fs/promises";\n\n' : ""}const response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: {\n    Authorization: \`Bearer \${process.env.AZIZAM_API_KEY}\`,\n    "Content-Type": "application/json",\n  },\n  body: JSON.stringify(${JSON.stringify(body, null, 2)}),\n});\nif (!response.ok) throw new Error(await response.text());\n${isBinary ? 'await writeFile("speech.mp3", Buffer.from(await response.arrayBuffer()));' : "console.log(await response.json());"}`,
    },
  ];
}

/** Copy one model name. A refused clipboard leaves a field to copy by hand. */
function CopyModelName({ row }: { row: CatalogRow }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(resetTimer.current), []);
  async function copy() {
    const ok = await copyText(row.id);
    setState(ok ? "copied" : "failed");
    window.clearTimeout(resetTimer.current);
    if (ok) resetTimer.current = window.setTimeout(() => setState("idle"), 1500);
  }
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="relative z-10 shrink-0"
        aria-label={`Copy model name ${row.id}`}
        onClick={() => void copy()}
      >
        {state === "copied" ? (
          <Check aria-hidden="true" className="text-success" />
        ) : (
          <Copy aria-hidden="true" />
        )}
      </Button>
      <span role="status" className="sr-only">
        {state === "copied" ? `${row.id} copied` : ""}
      </span>
      {state === "failed" && (
        <div className="bg-popover relative z-10 w-full min-w-0 space-y-2 rounded-panel border p-3">
          <p role="alert" className="text-destructive text-meta">
            Copy failed. Select the model name and copy it yourself.
          </p>
          <Input
            aria-label="Model name for manual copy"
            readOnly
            value={row.id}
            autoFocus
            onFocus={(event) => event.currentTarget.select()}
            onBlur={() => setState("idle")}
          />
        </div>
      )}
    </>
  );
}

/**
 * One model name. The name opens Details; the copy button sits beside it.
 * The second line carries what tells options apart: the model behind the
 * name and its description, unless every row in the task shares it.
 */
function ModelRow({
  row,
  sharedDescriptions,
  onDetail,
}: {
  row: CatalogRow;
  sharedDescriptions: Set<string>;
  onDetail: (row: CatalogRow) => void;
}) {
  const title = modelTitle(row);
  // Facts on one line are joined by " · ", so a sentence drops its full stop.
  const description =
    row.description && !sharedDescriptions.has(row.description)
      ? modelDescription(row.description)?.replace(/\.$/, "")
      : undefined;
  const provider = providerLabel(row);
  const tags = (row.tags ?? []).map((tag) => tagLabels[tag]).join(", ");
  const meta = [title, provider && `via ${provider}`, description, tags].filter(
    (part): part is string => Boolean(part),
  );
  const cost = costLabel(row);
  const status = statusWords(row).label;
  const usual = availabilityState(row) === "ready" || status === "Ready to start";
  // A phone has one line for facts: a state that needs attention first, and a
  // price only when it is not the usual local "no provider charge".
  const phoneFacts = [
    usual ? null : status,
    ...meta,
    isLocalFree(row) ? null : cost,
  ].filter((part): part is string => Boolean(part));
  return (
    <li
      data-slot="list-row"
      data-model={row.id}
      className={cn(
        "hover:bg-muted/50 relative flex flex-wrap items-center gap-3 px-4 py-1.5 transition-colors duration-(--motion-fast) max-sm:min-h-(--row-phone-2)",
        meta.length ? "min-h-(--row-2)" : "min-h-(--row-1)",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <p className="min-w-0 truncate">
            <button
              type="button"
              aria-haspopup="dialog"
              onClick={() => onDetail(row)}
              className="text-body rounded-control font-mono font-medium after:absolute after:inset-0 after:content-[''] hover:underline focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-ring"
            >
              {row.id}
            </button>
          </p>
          {row.recommended && (
            <Badge variant="secondary" className="shrink-0">
              Recommended
            </Badge>
          )}
        </div>
        {meta.length > 0 && (
          <p
            className="text-muted-foreground text-meta mt-0.5 truncate max-sm:hidden"
            title={meta.join(" · ")}
          >
            {meta.join(" · ")}
          </p>
        )}
        <p className="text-muted-foreground text-meta mt-0.5 truncate sm:hidden">
          {phoneFacts.length ? phoneFacts.join(" · ") : cost}
        </p>
      </div>
      <div className="text-muted-foreground text-meta flex shrink-0 items-center gap-4 max-sm:hidden">
        {/* The usual case (local, ready) is quieter, so a price or a
            problem stands out in a long list. */}
        <span
          className={cn(
            "line-clamp-2 w-56 text-right",
            isLocalFree(row) ? "text-subtle-foreground" : "text-foreground",
          )}
          title={cost}
        >
          {cost}
        </span>
        <span className={cn("w-32", usual ? "text-subtle-foreground" : "text-foreground")}>
          <QuietStatus row={row} />
        </span>
      </div>
      <CopyModelName row={row} />
    </li>
  );
}

function TaskSection({
  task,
  rows,
  onDetail,
}: {
  task: Task;
  rows: CatalogRow[];
  onDetail: (row: CatalogRow) => void;
}) {
  const counts = new Map<string, number>();
  for (const row of rows) {
    if (row.description) counts.set(row.description, (counts.get(row.description) ?? 0) + 1);
  }
  // A description two names share tells neither apart (VOICE rule 2), and a
  // task's only name needs none beside the task's own sentence.
  const shared = new Set(
    [...counts]
      .filter(([, count]) => count > 1 || rows.length === 1)
      .map(([text]) => text),
  );
  const headingId = `models-${task}`;
  return (
    <section aria-labelledby={headingId} id={`task-${task}`} className="scroll-mt-20">
      <div className="bg-muted/40 flex min-h-9 flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b px-4 py-2">
        <h2 id={headingId} className="text-body font-medium">
          {taskLabels[task]}
          <span className="text-muted-foreground ml-2 font-normal">{rows.length}</span>
        </h2>
        <p className="text-muted-foreground text-meta min-w-0">
          {taskExplainers[task]}
        </p>
      </div>
      <ul role="list" aria-labelledby={headingId} className="divide-y border-b last:border-b-0">
        {rows.map((row) => (
          <ModelRow key={row.id} row={row} sharedDescriptions={shared} onDetail={onDetail} />
        ))}
      </ul>
    </section>
  );
}

function LoadingCatalog() {
  return (
    <div aria-label="Loading models" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-8 w-80 max-sm:h-11" />
      </div>
      <div className="space-y-px overflow-hidden rounded-panel border">
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-(--row-2) rounded-none max-sm:h-(--row-phone-2)" />
        ))}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 py-2 sm:flex-row sm:gap-4">
      <dt className="text-muted-foreground text-meta sm:w-36 sm:shrink-0">{label}</dt>
      <dd className="text-body min-w-0 break-words">{value}</dd>
    </div>
  );
}

const technicalTermHints = [
  {
    pattern: /\bcontext window\b/i,
    hint: "Context window is how much text a model can handle in one request, including its reply.",
  },
  {
    pattern: /\bquantization\b/i,
    hint: "Quantization makes a model smaller and uses less memory. It can affect answer quality.",
  },
  {
    pattern: /\btokens per second\b/i,
    hint: "Tokens per second measures how fast text is produced. A token is a small piece of text.",
  },
];

function DetailSheet({
  row,
  onOpenChange,
  viewerAdmin,
  knownNames,
}: {
  row: CatalogRow | null;
  onOpenChange: (open: boolean) => void;
  viewerAdmin: boolean;
  knownNames: ReadonlySet<string>;
}) {
  const action = row ? availabilityAction(row, viewerAdmin) : null;
  const timing = row ? timingLabel(row) : undefined;
  const locality = row ? localityLabel(row) : undefined;
  const title = row ? modelTitle(row) : undefined;
  // An alias says what it points at, when that is a name the person can use too
  // (owners see every target under Technical details); every row says which
  // other names reach the same model.
  const pointsTo = row?.alias
    ? [...new Set(Object.values(row.resolves_to))].filter((target) => viewerAdmin || knownNames.has(target))
    : [];
  const sameModelAs = row ? (row.same_model_as ?? []) : [];
  return (
    <Sheet open={!!row} onOpenChange={onOpenChange}>
      <SheetContent className="w-[92vw] overflow-y-auto sm:max-w-xl">
        {row && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle className="min-w-0 font-mono [overflow-wrap:anywhere]">{row.id}</SheetTitle>
              <SheetDescription>
                {[title, modelDescription(row.description)].filter(Boolean).join(". ")}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <dl className="divide-y">
                <Fact label="Task" value={taskLabel(row)} />
                {pointsTo.length > 0 && <Fact label="Points to" value={pointsTo.join(", ")} />}
                {sameModelAs.length > 0 && (
                  <Fact label="Same model as" value={sameModelAs.join(", ")} />
                )}
                {locality && <Fact label="Runs on" value={locality} />}
                <Fact label="Price" value={costLabel(row)} />
                {timing && <Fact label="Time per request" value={timing} />}
                {row.tags && row.tags.length > 0 && (
                  <Fact label="Can also" value={row.tags.map((tag) => tagLabels[tag]).join(", ")} />
                )}
                {(row.io.input.length > 0 || row.io.output.length > 0) && (
                  <Fact
                    label="What you send and receive"
                    value={`${row.io.input.join(", ") || "—"} to ${row.io.output.join(", ") || "—"}`}
                  />
                )}
              </dl>
              <section className="space-y-3">
                <h3 className="text-body font-medium">Availability</h3>
                <div className="flex flex-col items-start gap-3">
                  <Availability row={row} />
                  {action && (
                    <Button variant="outline" size="sm" asChild>
                      <a href={action.href}>{action.label}</a>
                    </Button>
                  )}
                </div>
              </section>
              {viewerAdmin &&
                (Object.keys(row.resolves_to).length > 0 ||
                  row.technical_description ||
                  row.engine_label) && (
                  <section className="space-y-2">
                    <h3 className="text-body font-medium">Technical details</h3>
                    <dl className="divide-y">
                      <Fact label="Engine or provider" value={engineLabel(row)} />
                      {Object.keys(row.resolves_to).length > 0 && (
                        <Fact
                          label="Sends requests to"
                          value={Object.entries(row.resolves_to)
                            .map(([op, target]) => `${op}: ${target}`)
                            .join(", ")}
                        />
                      )}
                      {row.technical_description && (
                        <Fact
                          label="How this model runs"
                          value={[
                            row.technical_description,
                            ...technicalTermHints
                              .filter(({ pattern }) => pattern.test(row.technical_description ?? ""))
                              .map(({ hint }) => hint),
                          ].join(" ")}
                        />
                      )}
                    </dl>
                  </section>
                )}
              <section className="space-y-3">
                <h3 className="text-body font-medium">Use it from an app</h3>
                <p className="text-muted-foreground text-meta">
                  Set your OpenAI connection address and model name with these examples.
                  Replace YOUR_KEY with your own key in your server-side app.
                </p>
                <div className="[&_pre]:break-all [&_pre]:whitespace-pre-wrap">
                  <CodeBlock key={row.id} snippets={appSnippetsFor(row, window.location.origin)} />
                </div>
              </section>
              <section className="space-y-3">
                <h3 className="text-body font-medium">Example for app setup</h3>
                <div className="[&_pre]:break-all [&_pre]:whitespace-pre-wrap">
                  {snippetsFor(row, window.location.origin).length > 0 ? (
                    <CodeBlock snippets={snippetsFor(row, window.location.origin)} />
                  ) : (
                    <p className="text-muted-foreground text-meta">
                      No example was provided. Use the model name above when connecting your app.
                    </p>
                  )}
                </div>
              </section>
              <section>
                <h3 className="text-body font-medium">Recent activity</h3>
                {row.recent_receipts.length ? (
                  <ul className="mt-3 space-y-2 text-meta">
                    {row.recent_receipts.slice(0, 5).map((receipt, index) => (
                      <li
                        key={readString(receipt.request_id) ?? index}
                        className="rounded-panel border p-3"
                      >
                        {[
                          readString(receipt.status)?.toLowerCase() === "unknown"
                            ? undefined
                            : readString(receipt.status),
                          readNumber(receipt.latency_ms) !== null
                            ? `${readNumber(receipt.latency_ms)} ms`
                            : undefined,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                        {readString(receipt.request_id) && (
                          <a
                            className="mt-2 block min-h-8 content-center underline underline-offset-4 max-sm:min-h-11"
                            href={`/usage/?receipt=${encodeURIComponent(readString(receipt.request_id) ?? "")}`}
                          >
                            View request details
                          </a>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground mt-2 text-meta">
                    No recent requests were reported. Use this model in an app, then check Usage for results.
                  </p>
                )}
              </section>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

/** The address an app pastes beside a model name. */
function GatewayAddress({ address }: { address: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <div className="text-meta flex min-w-0 flex-wrap items-center gap-2 max-sm:w-full">
      <span className="text-muted-foreground shrink-0">{preferredTerms.connectionAddress}</span>
      <code className="min-w-0 truncate font-mono">{address}</code>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={`Copy ${preferredTerms.connectionAddress.toLowerCase()}`}
        onClick={async () => {
          const ok = await copyText(address);
          setCopied(ok);
          setFailed(!ok);
          window.clearTimeout(timer.current);
          if (ok) timer.current = window.setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? <Check aria-hidden="true" className="text-success" /> : <Copy aria-hidden="true" />}
      </Button>
      <span role="status" className="sr-only">
        {copied ? `${preferredTerms.connectionAddress} copied` : ""}
      </span>
      {failed && (
        <div className="w-full min-w-0 space-y-2">
          <p role="alert">Could not copy. Select the address below and copy it yourself.</p>
          <Input
            aria-label="Gateway address for manual copy"
            readOnly
            value={address}
            onFocus={(event) => event.currentTarget.select()}
          />
        </div>
      )}
    </div>
  );
}

type PriceFilter = "all" | CostKind;
type TaskFilter = "all" | Task;

const priceFilterLabels: Record<PriceFilter, string> = {
  all: "Any price",
  none: "No provider charge",
  paid: "Paid",
  unknown: "Cost not recorded",
};

export default function ModelsClient() {
  const viewer = useViewer();
  const [query, setQuery] = useState("");
  const [price, setPrice] = useState<PriceFilter>("all");
  const [taskFilter, setTaskFilter] = useState<TaskFilter>("all");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailNote, setDetailNote] = useState<string | null>(null);
  const {
    data: catalogData,
    isPending: catalogPending,
    isError: catalogError,
    isFetching: catalogFetching,
    refetch: refetchCatalog,
  } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
  });
  const { data: discovery } = useQuery({
    queryKey: ["gateway", "discovery"],
    queryFn: accountApi.discovery,
  });
  const address =
    discovery?.recipes?.openai?.canonical_base_url ??
    (typeof window === "undefined" ? "" : `${window.location.origin}/openai/v1`);

  // Aliases first, then exact models; a name listed as both appears once.
  const allRows = useMemo(() => {
    const seen = new Set<string>();
    const rows: CatalogRow[] = [];
    for (const row of [...(catalogData?.aliases ?? []), ...(catalogData?.models ?? [])]) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      rows.push(row);
    }
    return rows;
  }, [catalogData]);
  const knownNames = useMemo(() => new Set(allRows.map((row) => row.id)), [allRows]);
  const detail = allRows.find((row) => row.id === detailId) ?? null;
  useEffect(() => {
    if (detailId !== null && catalogData && !detail) {
      setDetailId(null);
      setDetailNote(detailId);
    }
  }, [catalogData, detail, detailId]);
  useEffect(() => {
    if (detailNote && allRows.some((row) => row.id === detailNote)) setDetailNote(null);
  }, [allRows, detailNote]);
  function selectDetail(row: CatalogRow) {
    setDetailNote(null);
    setDetailId(row.id);
  }
  // ?model=<name> opens that model; ?capability=<task> scrolls to its section.
  const appliedDeepLink = useRef(false);
  useEffect(() => {
    if (!catalogData || appliedDeepLink.current) return;
    appliedDeepLink.current = true;
    const params = new URLSearchParams(window.location.search);
    const model = allRows.find((row) => row.id === params.get("model"));
    if (model) setDetailId(model.id);
    const task = taskOrder.find((value) => value === params.get("capability"));
    if (task && allRows.some((row) => rowTask(row) === task)) setTaskFilter(task);
  }, [catalogData, allRows]);

  const priceKinds = (["none", "paid", "unknown"] as CostKind[]).filter((kind) =>
    allRows.some((row) => costKind(row) === kind),
  );
  const tasksPresent = taskOrder.filter((task) => allRows.some((row) => rowTask(row) === task));
  // Facet counts respect search and the other filter, not their own selection.
  const searched = allRows.filter((row) => itemMatches(row, query.trim()));
  const taskCandidates = searched.filter((row) => price === "all" || costKind(row) === price);
  const priceCandidates = searched.filter((row) => taskFilter === "all" || rowTask(row) === taskFilter);
  const taskCount = (task: TaskFilter) =>
    task === "all" ? taskCandidates.length : taskCandidates.filter((row) => rowTask(row) === task).length;
  const priceCount = (kind: PriceFilter) =>
    kind === "all" ? priceCandidates.length : priceCandidates.filter((row) => costKind(row) === kind).length;
  const visible = allRows.filter(
    (row) =>
      itemMatches(row, query.trim()) &&
      (price === "all" || costKind(row) === price) &&
      (taskFilter === "all" || rowTask(row) === taskFilter),
  );
  // Within a task: the recommended name, other names, then exact models.
  const rank = (row: CatalogRow) =>
    row.recommended ? 0 : row.alias && !row.secondary_profile ? 1 : row.alias ? 2 : 3;
  const groups = taskOrder
    .map((task) => ({
      task,
      rows: visible
        .filter((row) => rowTask(row) === task)
        .map((row, index) => ({ row, index }))
        .sort((a, b) => rank(a.row) - rank(b.row) || a.index - b.index)
        .map(({ row }) => row),
    }))
    .filter((group) => group.rows.length > 0);
  const filtered = Boolean(query.trim()) || price !== "all" || taskFilter !== "all";

  return (
    <div>
      <PageHeader
        title="Models"
        subtitle="Names your apps put in the model field."
      />
      {catalogPending ? (
        <LoadingCatalog />
      ) : catalogError ? (
        <ErrorState
          message="We couldn't load the models. Try again."
          onRetry={() => refetchCatalog()}
          retrying={catalogFetching}
        />
      ) : allRows.length === 0 ? (
        <EmptyState
          icon={SlidersHorizontal}
          title="No models are available yet"
          description={
            viewer.admin
              ? "Connect an engine to run models on your equipment, or connect an online provider."
              : "Ask the owner to connect an engine or online provider and give you access to a model."
          }
          action={
            <Button variant="outline" asChild>
              <Link prefetch={false} href={viewer.admin ? "/engines/" : "/"}>
                {viewer.admin ? "Connect an engine" : "Review setup"}
              </Link>
            </Button>
          }
        />
      ) : (
        <div className="space-y-4">
          <section
            aria-label="Model filters"
            className="flex flex-wrap items-center gap-3"
          >
            <label className="relative w-full min-w-0 sm:w-80">
              <span className="sr-only">Search models</span>
              <Search
                aria-hidden="true"
                className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                size="sm"
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search models, tasks or providers"
                className="pl-9"
              />
            </label>
            {tasksPresent.length > 1 && (
              <Select value={taskFilter} onValueChange={(value) => setTaskFilter(value as TaskFilter)}>
                <SelectTrigger size="sm" aria-label="Task" className="max-sm:w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All tasks ({taskCount("all")})</SelectItem>
                  {tasksPresent.map((task) => (
                    <SelectItem key={task} value={task}>
                      {taskLabels[task]} ({taskCount(task)})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {priceKinds.length > 1 && (
              <Select value={price} onValueChange={(value) => setPrice(value as PriceFilter)}>
                <SelectTrigger size="sm" aria-label="Price" className="max-sm:w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(["all", ...priceKinds] as PriceFilter[]).map((value) => (
                    <SelectItem key={value} value={value}>
                      {priceFilterLabels[value]} ({priceCount(value)})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {address && (
              <div className="sm:ml-auto max-sm:w-full">
                <GatewayAddress address={address} />
              </div>
            )}
          </section>
          {groups.length === 0 ? (
            <EmptyState
              icon={Search}
              title="No models match"
              description="Try another name, or clear the filters."
              action={
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setQuery("");
                    setPrice("all");
                    setTaskFilter("all");
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <div aria-label="Model names by task" role="region" className="bg-card rounded-panel overflow-hidden border">
              <div
                aria-hidden="true"
                className="text-muted-foreground text-label flex h-9 items-center gap-3 border-b px-4 max-sm:hidden"
              >
                <span className="flex-1">Model name</span>
                <span className="flex gap-4">
                  <span className="w-56 text-right">Price</span>
                  <span className="w-32">Status</span>
                </span>
                <span className="w-8" />
              </div>
              {groups.map((group) => (
                <TaskSection
                  key={group.task}
                  task={group.task}
                  rows={group.rows}
                  onDetail={selectDetail}
                />
              ))}
            </div>
          )}
          <p className="text-muted-foreground text-meta">
            <span role="status" aria-live="polite" aria-atomic="true">
              {visible.length === 0
                ? `No models match. 0 of ${allRows.length} model names.`
                : filtered
                  ? `${visible.length} of ${allRows.length} model names.`
                  : `${allRows.length} model names.`}
            </span>
            {" "}
            <Link prefetch={false} href="/connect/" className="underline underline-offset-4">
              How to connect an app
            </Link>
          </p>
        </div>
      )}
      {detailNote && (
        <div role="status" className="text-meta flex items-center gap-2">
          <p>This model is no longer listed. Choose another model, or review setup if the list is empty.</p>
          <Button variant="ghost" size="sm" onClick={() => setDetailNote(null)}>
            Dismiss
          </Button>
        </div>
      )}
      <DetailSheet
        row={detail}
        onOpenChange={(open) => !open && setDetailId(null)}
        viewerAdmin={viewer.admin}
        knownNames={knownNames}
      />
    </div>
  );
}
