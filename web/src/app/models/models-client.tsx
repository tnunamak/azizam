"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Search,
  SlidersHorizontal,
  AudioLines,
  GitBranch,
  Image,
  Layers,
  ListFilter,
  MessageSquare,
  Mic,
  Music2,
  Video,
  Waves,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { productApi, type CatalogRow, type ModelTag } from "@/lib/product-api";
import {
  capabilityLabels as terms,
  preferredTerms,
  taskNames,
} from "@/lib/terms";
import { availabilityReason } from "@/lib/status-copy";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useViewer } from "@/components/shell/session";
import { FilterToggle } from "@/components/product/filter-toggle";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CodeBlock,
  EmptyState,
  ErrorState,
  PageHeader,
  StatusPill,
} from "@/components/product";

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

type ProductCapability =
  | "music"
  | "sfx"
  | "speech"
  | "transcribe"
  | "chat"
  | "image"
  | "video"
  | "decide"
  | "embed"
  | "rerank"
  | "audio";

const productCapabilityOrder: ProductCapability[] = [
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

const capabilityLabels: Record<ProductCapability, string> = {
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

/**
 * What apps use embeddings and decisions for, in the words Try uses
 * (lib/terms.ts taskNames), in place of the server's "Embeddings" and
 * "Decisions".
 */
const PLAIN_TASK: Partial<Record<ProductCapability, string>> = {
  decide: taskNames.decisions,
  embed: taskNames.embeddings,
};

/** The name for one capability: the plain task name, else the server's label. */
function capabilityName(cap: ProductCapability, serverLabel?: string | null) {
  return PLAIN_TASK[cap] ?? serverLabel ?? capabilityLabels[cap];
}

/** Input and output kinds as people read them ("text → numbers"). */
const MODALITY_WORDS: Record<string, string> = {
  embedding: "numbers",
  embeddings: "numbers",
};

// The server owns taxonomy and labels; this map only selects a glyph.
const capabilityGlyphs = {
  chat: MessageSquare,
  image: Image,
  video: Video,
  music: Music2,
  sfx: Waves,
  speech: AudioLines,
  transcribe: Mic,
  decide: GitBranch,
  embed: Layers,
  rerank: ListFilter,
};

function CatalogIcon({ row }: { row: CatalogRow }) {
  const key =
    row.icon_key ?? row.capabilities[0]?.icon_key ?? primaryCapability(row);
  const Icon = capabilityGlyphs[key as keyof typeof capabilityGlyphs] ?? Layers;
  return (
    <span
      role="img"
      aria-label={primaryCapabilityLabel(row)}
      className="text-muted-foreground inline-flex size-4 shrink-0 items-center justify-center"
    >
      <Icon aria-hidden="true" className="size-4" />
    </span>
  );
}

// The server classifies each row from its /v1/models type and modalities.
function primaryCapability(row: CatalogRow): ProductCapability {
  return (row.capability ??
    row.capabilities[0]?.capability ??
    "chat") as ProductCapability;
}

function primaryCapabilityLabel(row: CatalogRow): string {
  return capabilityName(
    primaryCapability(row),
    row.capability_title ?? row.capabilities[0]?.label,
  );
}

function hasCapability(
  row: CatalogRow,
  capability: ProductCapability,
): boolean {
  return (
    primaryCapability(row) === capability ||
    row.capabilities.some((entry) => entry.capability === capability)
  );
}

const tagLabels: Record<ModelTag, string> = {
  reasoning: "Reasoning",
  "tool-use": "Tool use",
  vision: "Vision",
  "file-input": "File input",
};

// The same cells form a task line and an inline alias/cost/status line on phones.
const phoneRowClass =
  "max-sm:relative max-sm:has-[input]:h-auto max-sm:grid max-sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] max-sm:grid-rows-2 max-sm:h-(--row-phone-2) max-sm:items-center max-sm:gap-x-2 max-sm:px-4 max-sm:py-2";
const phoneCellClass = "max-sm:h-auto! max-sm:p-0! max-sm:min-w-0";
const phoneTaskClass = "flex min-w-0 items-center gap-3 max-sm:col-span-3 max-sm:row-start-1 max-sm:pr-24";
const phoneCostClass = "max-sm:col-start-2 max-sm:row-start-2 max-sm:truncate max-sm:text-left";
const phoneStatusClass = "max-sm:col-start-3 max-sm:row-start-2";
const phoneActionsClass = "max-sm:absolute max-sm:right-2 max-sm:top-0";

const rowTaskNames: Record<ProductCapability, string> = {
  chat: taskNames.chat,
  image: taskNames.image,
  video: taskNames.video,
  music: taskNames.music,
  sfx: taskNames.sound_effects,
  speech: taskNames.speech,
  audio: taskNames.audio,
  transcribe: taskNames.transcribe,
  decide: taskNames.decisions,
  embed: taskNames.embeddings,
  rerank: taskNames.reranking,
};

function rowTaskName(row: CatalogRow): string {
  return rowTaskNames[primaryCapability(row)] ?? primaryCapabilityLabel(row);
}

// Inputs -> outputs and the chat capability tags /v1/models reports.
function ModelFacts({
  row,
  className,
}: {
  row: CatalogRow;
  className?: string;
}) {
  const inputs = row.architecture?.input_modalities ?? [];
  const outputs = row.architecture?.output_modalities ?? [];
  const tags = row.tags ?? [];
  const showIo = inputs.length > 0 && outputs.length > 0;
  if (!showIo && tags.length === 0) return null;
  return (
    <ul
      aria-label="Model facts"
      className={cn("flex flex-wrap items-center gap-1", className)}
    >
      {showIo && (
        <li>
          <Badge
            variant="outline"
            className="text-muted-foreground font-normal"
          >
            {inputs.map((word) => MODALITY_WORDS[word] ?? word).join(", ")}
            <span aria-hidden="true">→</span>
            <span className="sr-only">to</span>
            {outputs.map((word) => MODALITY_WORDS[word] ?? word).join(", ")}
          </Badge>
        </li>
      )}
      {tags.map((tag) => (
        <li key={tag}>
          <Badge variant="secondary">{tagLabels[tag]}</Badge>
        </li>
      ))}
    </ul>
  );
}

function descriptionFor(row: CatalogRow): string {
  return row.description ?? "";
}

function titleFor(row: CatalogRow): string | undefined {
  // A title that only repeats the capability ("Embed" for Embeddings) adds
  // nothing; the plain capability name stands in for it.
  const names = [
    primaryCapabilityLabel(row),
    row.capability_title,
    row.capabilities[0]?.label,
  ].filter((name): name is string => Boolean(name));
  const repeatsCapability = (value: string) => {
    const normalize = (part: string) => {
      const word = part
        .trim()
        .toLowerCase()
        .replace(/[^a-z]/g, "");
      return word === "embed" || word === "embedding" ? "embeddings" : word;
    };
    const parts = value.split("·").map(normalize).filter(Boolean);
    return (
      parts.length > 0 &&
      parts.every((part) => names.some((name) => part === normalize(name)))
    );
  };

  const title = row.title ?? row.display_name;
  if (title && !repeatsCapability(title)) return title;
  return undefined;
}

function hasNoProviderCharge(row: CatalogRow): boolean {
  return row.locality === "local" || row.cost.kind === "free";
}

function costLabel(row: CatalogRow): string | undefined {
  if (row.locality === "remote" && row.cost.kind === "unknown")
    return "Cost not recorded";
  if (row.cost.kind === "paid") {
    return row.cost.price === null
      ? "Paid · price unavailable"
      : `Paid · ${String(row.cost.price)}`;
  }
  return undefined;
}

function localityLabel(row: CatalogRow): string | undefined {
  if (row.locality === "local") return "Local";
  if (row.locality === "remote") return "Remote";
  return undefined;
}

function engineLabel(row: CatalogRow): string {
  return (
    row.engine_label ??
    // A local row's provider field is a service id, not a name.
    (row.locality === "remote" ? row.provider : undefined) ??
    "Engine or provider not specified"
  );
}

function summaryEngineLabel(row: CatalogRow): string {
  return engineLabel(row);
}

function timingLabel(row: CatalogRow): string | undefined {
  const value = row.timing.typical_seconds;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return undefined;
  }
  const duration =
    value < 1
      ? `~${Math.round(value * 1000)} ms`
      : `~${value.toFixed(value >= 10 ? 0 : 1)} s`;
  const source =
    row.timing.source === "receipts"
      ? "Measured"
      : row.timing.source === "configured"
        ? "Typical"
        : undefined;
  return source ? `${duration} · ${source}` : duration;
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

// Quiet summary states render no status value in catalog rows.
function hasSummaryStatus(row: CatalogRow): boolean {
  return availabilityState(row) !== "ready";
}

function evidenceLabel(row: CatalogRow): string | undefined {
  const source = row.availability.evidence?.source;
  if (row.availability.measured) {
    if (source === "registry") return "From configuration";
  }
  return undefined;
}

function configurationLabel(row: CatalogRow): string | undefined {
  if (row.availability.configured === false) return "Catalog only";
  if (row.availability.configured === true) return "Configured";
  return undefined;
}

function enabledLabel(row: CatalogRow): string | null {
  if (row.availability.enabled === true) return "Enabled";
  if (row.availability.enabled === false) return "Disabled";
  return null;
}

function availabilityAction(
  row: CatalogRow,
  viewerAdmin: boolean,
): { label: string; href: string } | null {
  const configHref =
    row.locality === "remote" ? "/providers/" : "/engines/";
  if (viewerAdmin) {
    if (row.availability.configured === false) {
      return {
        label:
          row.locality === "remote"
            ? "Connect a provider"
            : "Connect an engine",
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

function AvailabilityFacts({ row }: { row: CatalogRow }) {
  const enabled = enabledLabel(row);
  return (
    <div className="flex flex-wrap gap-2 text-meta">
      <Badge variant="secondary">Allowed by your access</Badge>
      {configurationLabel(row) && (
        <Badge variant="outline">{configurationLabel(row)}</Badge>
      )}
      {evidenceLabel(row) && (
        <Badge variant="outline">{evidenceLabel(row)}</Badge>
      )}
      {enabled && <Badge variant="outline">{enabled}</Badge>}
    </div>
  );
}

function CopyModelName({ row }: { row: CatalogRow }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  // The "Copied" mark ends on a timer; a timer that outlives the row would set state on nothing.
  const resetTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(resetTimer.current), []);
  async function copyName() {
    try {
      await navigator.clipboard.writeText(row.id);
      setCopyError(false);
      setCopied(true);
      window.clearTimeout(resetTimer.current);
      resetTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
      setCopyError(true);
    }
  }
  return (
    <div className={cn(
      "min-w-0 max-sm:col-start-1",
      copyError
        ? "max-sm:col-span-3 max-sm:row-start-3"
        : "max-sm:row-start-2",
      !copyError && !costLabel(row) && (
        hasSummaryStatus(row) ? "max-sm:col-span-2" : "max-sm:col-span-3"
      ),
    )}>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          void copyName();
        }}
        aria-label={`Copy model name ${row.id}`}
        className="text-muted-foreground bg-muted hover:bg-muted/80 flex h-8 max-sm:min-h-11 sm:-my-1 max-sm:-my-3 max-sm:bg-transparent max-sm:px-0 max-w-full min-w-0 items-center justify-between gap-2 rounded-control px-3 text-left font-mono text-meta transition"
      >
        <code className="truncate">{row.id}</code>
        {copied ? (
          <Check aria-label="Model name copied" className="size-4 shrink-0" />
        ) : (
          <Copy aria-hidden="true" className="size-4 shrink-0" />
        )}
      </button>
      {copyError && (
        <div
          className="mt-2 space-y-2"
          onClick={(event) => event.stopPropagation()}
        >
          <p role="alert" className="text-destructive text-meta">
            Copy failed. Select the model name and copy it manually.
          </p>
          <Input
            aria-label="Model name for manual copy"
            readOnly
            value={row.id}
            onFocus={(event) => event.currentTarget.select()}
          />
        </div>
      )}
    </div>
  );
}

function itemMatches(row: CatalogRow, query: string): boolean {
  if (!query) return true;
  const haystack = [
    row.id,
    row.name,
    row.display_name,
    titleFor(row),
    descriptionFor(row),
    primaryCapabilityLabel(row),
    rowTaskName(row),
    ...(row.use_names ?? []),
    row.recommended_model,
    row.provider,
    row.engine_label,
    ...row.operations,
    ...row.capabilities.map((cap) => cap.label),
    ...Object.values(row.resolves_to),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function shellSingleQuote(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`;
}

function exampleBody(row: CatalogRow): Record<string, unknown> {
  return row.examples[0]?.body ?? { model: row.use.model };
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

function Availability({
  row,
  showReason = true,
}: {
  row: CatalogRow;
  showReason?: boolean;
}) {
  const state = availabilityState(row);
  if (state === "unknown" || state === "not_measured") {
    return <span className="text-muted-foreground text-meta whitespace-normal">
      <span>{state === "not_measured" || !row.availability.measured ? "Not checked yet" : "Couldn\'t check"}</span>
      {!showReason && <> · <Link prefetch={false} href="/status/" onClick={(event) => event.stopPropagation()} className="underline underline-offset-4">Check status</Link></>}
    </span>;
  }
  return (
    <StatusPill
      status={state}
      reason={
        showReason
          ? availabilityReason(state, row.availability.reason_code ?? state)
          : undefined
      }
      summary={!showReason}
    />
  );
}

function AliasOptionRow({
  row,
  onDetail,
  showCost,
  showStatus,
}: {
  row: CatalogRow;
  onDetail: (row: CatalogRow) => void;
  showCost: boolean;
  showStatus: boolean;
}) {
  return (
    <TableRow onClick={() => onDetail(row)} className={cn("cursor-pointer h-(--row-2)", phoneRowClass)}>
      <TableCell className={cn("h-(--row-2) py-1! max-sm:contents", phoneCellClass)}>
        <h3 className={cn("text-body font-medium", phoneTaskClass)} title={rowTaskName(row)}>
          <span className="truncate">{rowTaskName(row)}</span>
        </h3>
        <CopyModelName row={row} />
      </TableCell>
      <TableCell className={cn("h-(--row-2) py-1!", phoneCellClass, "max-sm:hidden")}>
        <h3 className="text-body font-medium">{titleFor(row) ?? ""}</h3>
        <p className="text-muted-foreground text-meta mt-0.5 max-w-80 truncate" title={descriptionFor(row)}>
          {descriptionFor(row)}
        </p>
      </TableCell>
      <TableCell className="py-1! max-sm:hidden"><ModelFacts row={row} /></TableCell>
      {showCost && <TableCell numeric title={costLabel(row)} className={cn("py-1!", phoneCellClass, phoneCostClass)}>{costLabel(row)}</TableCell>}
      {showStatus && <TableCell className={cn("py-1!", phoneCellClass, phoneStatusClass)}>
        {hasSummaryStatus(row) && (
          <Availability row={row} showReason={false} />
        )}
      </TableCell>}
      <TableCell className={cn("py-1!", phoneCellClass, phoneActionsClass)}>
        <Button
          variant="ghost"
          size="icon"
          onClick={(event) => {
            event.stopPropagation();
            onDetail(row);
          }}
        >
          <span className="sr-only">Details</span>
          <ChevronRight aria-hidden="true" />
        </Button>
      </TableCell>
    </TableRow>
  );
}

function CapabilityRow({
  rows,
  onDetail,
  searching,
  showCost,
  showStatus,
}: {
  rows: CatalogRow[];
  onDetail: (row: CatalogRow) => void;
  searching: boolean;
  showCost: boolean;
  showStatus: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  // An unmarked catalog has a neutral first option, never an inferred recommendation.
  const primary = rows.find((row) => row.recommended) ?? rows[0];
  const alternatives = rows.filter((row) => row !== primary);
  const open = expanded || searching;
  const alternativesId = `model-alternatives-${primaryCapability(primary)}`;
  return (
    <>
    <TableBody className="max-sm:block" aria-label={`${primaryCapabilityLabel(primary)} models`}>
      <TableRow className={cn("cursor-pointer h-(--row-2)", phoneRowClass)} onClick={() => onDetail(primary)}>
        <TableCell className={cn("h-(--row-2) py-1! max-sm:contents", phoneCellClass)}>
          <div className={phoneTaskClass}>
            <CatalogIcon row={primary} />
            <h2 className="text-body truncate font-medium" title={rowTaskName(primary)}>
              {rowTaskName(primary)}
            </h2>
          </div>
          <CopyModelName row={primary} />
        </TableCell>
        <TableCell className="py-1! max-sm:hidden">
          <h3 className="text-body font-medium">{titleFor(primary)}</h3>
        </TableCell>
        <TableCell className="py-1! max-sm:hidden"><ModelFacts row={primary} /></TableCell>
        {showCost && <TableCell numeric title={costLabel(primary)} className={cn("py-1!", phoneCellClass, phoneCostClass)}>{costLabel(primary)}</TableCell>}
        {showStatus && <TableCell className={cn("py-1!", phoneCellClass, phoneStatusClass)}>
          {hasSummaryStatus(primary) && (
            <Availability row={primary} showReason={false} />
          )}
        </TableCell>}
        <TableCell className={cn("py-1!", phoneCellClass, phoneActionsClass)}>
          <div className="flex items-center justify-end gap-2">
            {alternatives.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                aria-label={`${alternatives.length} more ${primaryCapabilityLabel(primary)} models`}
                aria-expanded={open}
                aria-controls={alternativesId}
                onClick={(event) => {
                  event.stopPropagation();
                  setExpanded((value) => !value);
                }}
              >
                <span className="max-sm:sr-only">{alternatives.length} more</span>
                <ChevronDown
                  aria-hidden="true"
                  className={cn("size-4 transition", open && "rotate-180")}
                />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              onClick={(event) => {
                event.stopPropagation();
                onDetail(primary);
              }}
            >
              <span className="sr-only">Details</span>
              <ChevronRight aria-hidden="true" />
            </Button>
          </div>
        </TableCell>
      </TableRow>
    </TableBody>
      {open && alternatives.length > 0 && (
        <TableBody id={alternativesId} className="bg-muted/20 max-sm:block">
          {alternatives.map((row) => (
            <AliasOptionRow key={row.id} row={row} onDetail={onDetail} showCost={showCost} showStatus={showStatus} />
          ))}
        </TableBody>
      )}
    </>
  );
}

function LoadingCatalog() {
  return (
    <div aria-label="Loading models" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-8 max-sm:h-11 w-56" />
        <Skeleton className="h-8 max-sm:h-11 w-28" />
        <Skeleton className="h-8 max-sm:h-11 w-28" />
      </div>
      <div className="space-y-px overflow-hidden rounded-panel border">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-(--row-2) max-sm:h-(--row-phone-2) rounded-none" />
        ))}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-panel border p-3">
      <div className="text-muted-foreground text-meta">{label}</div>
      <div className="mt-1 font-medium break-words">{value}</div>
    </div>
  );
}

function DetailSheet({
  row,
  onOpenChange,
  viewerAdmin,
}: {
  row: CatalogRow | null;
  onOpenChange: (open: boolean) => void;
  viewerAdmin: boolean;
}) {
  const action = row ? availabilityAction(row, viewerAdmin) : null;
  const timing = row ? timingLabel(row) : undefined;
  const locality = row ? localityLabel(row) : undefined;
  const cost = row ? costLabel(row) : undefined;
  return (
    <Sheet open={!!row} onOpenChange={onOpenChange}>
      <SheetContent className="w-[92vw] overflow-y-auto sm:max-w-xl">
        {row && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>
                {titleFor(row) ?? primaryCapabilityLabel(row)}
              </SheetTitle>
              <SheetDescription>{descriptionFor(row)}</SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <div className="grid gap-3 text-meta sm:grid-cols-2">
                <Fact
                  label={row.alias ? "Model name" : "Exact model"}
                  value={row.id}
                />
                {engineLabel(row) !== titleFor(row) && (
                  <Fact label="Engine or provider" value={engineLabel(row)} />
                )}
                {locality && <Fact label="Location" value={locality} />}
                {cost && <Fact label="Cost" value={cost} />}
                {timing && (
                  <Fact
                    label={
                      row.timing.source === "receipts"
                        ? "Measured time"
                        : "Typical time"
                    }
                    value={timing}
                  />
                )}
              </div>
              {viewerAdmin && (Object.keys(row.resolves_to).length > 0 || row.technical_description) && (
                <section className="space-y-3">
                  <h3 className="font-medium">Technical details</h3>
                  <div className="grid gap-3 text-meta sm:grid-cols-2">
                    {Object.keys(row.resolves_to).length > 0 && (
                      <Fact
                        label="Resolves to"
                        value={Object.entries(row.resolves_to)
                          .map(([op, target]) => `${op}: ${target}`)
                          .join(", ")}
                      />
                    )}
                    {row.technical_description && (
                      <Fact
                        label="Technical description"
                        value={row.technical_description}
                      />
                    )}
                  </div>
                </section>
              )}
              <div>
                <h3 className="font-medium">Availability</h3>
                <div className="mt-3 space-y-3 rounded-panel border p-3">
                  <Availability row={row} />
                  <AvailabilityFacts row={row} />
                  {action && (
                    <Button variant="outline" size="sm" asChild>
                      <a href={action.href}>{action.label}</a>
                    </Button>
                  )}
                </div>
              </div>
              <div>
                <h3 className="font-medium">Names you can use</h3>
                {row.use_names?.length ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {row.use_names.map((name) => (
                      <Badge key={name} variant="secondary">
                        {name}
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="text-muted-foreground mt-2 text-meta">{row.id}</p>
                )}
              </div>
              {(row.io.input.length > 0 || row.io.output.length > 0) && <div>
                <h3 className="font-medium">Inputs and outputs</h3>
                {(row.io.input.length > 0 || row.io.output.length > 0) && (
                  <dl className="text-muted-foreground mt-2 space-y-1 text-meta">
                    {row.io.input.length > 0 && (
                      <div>
                        <dt className="inline">Inputs: </dt>
                        <dd className="inline">{row.io.input.join(", ")}</dd>
                      </div>
                    )}
                    {row.io.output.length > 0 && (
                      <div>
                        <dt className="inline">Outputs: </dt>
                        <dd className="inline">{row.io.output.join(", ")}</dd>
                      </div>
                    )}
                  </dl>
                )}
              </div>}
              <div>
                <h3 className="font-medium">Example request</h3>
                <div className="mt-3 [&_pre]:break-all [&_pre]:whitespace-pre-wrap">
                  <CodeBlock
                    snippets={snippetsFor(row, window.location.origin)}
                  />
                </div>
              </div>
              <div>
                <h3 className="font-medium">Recent receipts</h3>
                {row.recent_receipts.length ? (
                  <ul className="mt-3 space-y-2 text-meta">
                    {row.recent_receipts.slice(0, 5).map((receipt, index) => (
                      <li
                        key={readString(receipt.request_id) ?? index}
                        className="rounded-panel border p-3"
                      >
                        {[
                          readString(receipt.status)?.toLowerCase() ===
                          "unknown"
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
                            className="mt-2 block min-h-8 max-sm:min-h-11 content-center underline underline-offset-4"
                            href={`/usage/?receipt=${encodeURIComponent(readString(receipt.request_id) ?? "")}`}
                          >
                            View receipt
                          </a>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground mt-2 text-meta">
                    No recent receipts were reported for this model.
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

export default function ModelsClient() {
  const viewer = useViewer();
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState<ProductCapability | "all">(
    "all",
  );
  const [showNoProviderCharge, setShowNoProviderCharge] = useState(true);
  const [showPaid, setShowPaid] = useState(true);
  const [showUnknownCost, setShowUnknownCost] = useState(true);
  const [tableOpen, setTableOpen] = useState(false);
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
  const allRows = useMemo(
    () => [...(catalogData?.aliases ?? []), ...(catalogData?.models ?? [])],
    [catalogData],
  );
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
  const appliedDeepLink = useRef(false);
  useEffect(() => {
    if (!catalogData || appliedDeepLink.current) return;
    appliedDeepLink.current = true;
    const params = new URLSearchParams(window.location.search);
    const requestedCapability = params.get("capability");
    const initialCapability = productCapabilityOrder.find(
      (cap) => cap === requestedCapability,
    );
    if (initialCapability) setCapability(initialCapability);
    const model = allRows.find((row) => row.id === params.get("model"));
    if (model) {
      setQuery(model.id);
      if (!model.alias) setTableOpen(true);
    }
  }, [catalogData, allRows]);
  const sectionRows = allRows.filter(
    (row) => row.alias || row.secondary_profile,
  );
  const availableCapabilities = productCapabilityOrder.filter((cap) =>
    allRows.some((row) => hasCapability(row, cap)),
  );
  const passesFilters = (row: CatalogRow) => {
    if (!itemMatches(row, query)) return false;
    if (capability !== "all" && !hasCapability(row, capability)) {
      return false;
    }
    if (row.locality === "remote" && row.cost.kind === "unknown")
      return showUnknownCost;
    if (hasNoProviderCharge(row) && !showNoProviderCharge) return false;
    if (!hasNoProviderCharge(row) && row.cost.kind === "paid" && !showPaid)
      return false;
    return true;
  };
  const groupedAliases = productCapabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: sectionRows.filter(
        (row) => passesFilters(row) && primaryCapability(row) === cap,
      ),
    }))
    .filter((group) => group.rows.length > 0);
  const aliasRows = groupedAliases.flatMap((group) => group.rows);
  const sectionIds = new Set(aliasRows.map((row) => row.id));
  // All models lists the rows not already shown above, so nothing repeats.
  const otherModels = (catalogData?.models ?? []).filter((row) => !sectionIds.has(row.id));
  const modelRows = otherModels.filter(passesFilters);
  const aliasHasCost = aliasRows.some((row) => Boolean(costLabel(row)));
  const aliasHasStatus = aliasRows.some((row) => hasSummaryStatus(row));
  const modelHasCost = modelRows.some((row) => Boolean(costLabel(row)));
  const modelHasStatus = modelRows.some(hasSummaryStatus);
  const showLocalCostNote = [...aliasRows, ...(tableOpen ? modelRows : [])].some(
    (row) => row.locality === "local" && !costLabel(row),
  );

  return (
    <div className="space-y-(--space-section)">
      <PageHeader title="Models" answer="Pick a model name." />
      {catalogPending ? (
        <LoadingCatalog />
      ) : catalogError ? (
        <ErrorState
          message="Models catalog could not be loaded. Try again."
          onRetry={() => refetchCatalog()}
          retrying={catalogFetching}
        />
      ) : allRows.length === 0 ? (
        <EmptyState
          icon={SlidersHorizontal}
          title="No models are available yet"
          description="Ask the owner to connect models your access plan allows."
          action={
            <Button variant="outline" asChild>
              <Link prefetch={false} href="/">Review setup</Link>
            </Button>
          }
        />
      ) : (
        <>
          <section aria-label="Model filters" className="space-y-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
              <label className="relative min-w-0 flex-1 sm:max-w-80">
                <span className="sr-only">Search models</span>
                <Search
                  aria-hidden="true"
                  className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
                />
                <Input
                  size="sm"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={`Search ${preferredTerms.requestSelector.toLowerCase()}s or providers`}
                  className="pl-9"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2 pb-1">
              <FilterToggle
                className="px-3 text-meta [&>svg]:hidden"
                pressed={capability === "all"}
                onClick={() => setCapability("all")}
              >
                All
              </FilterToggle>
              {availableCapabilities.map((cap) => (
                <FilterToggle
                  key={cap}
                  className="px-3 text-meta [&>svg]:hidden"
                  pressed={capability === cap}
                  onClick={() => setCapability(cap)}
                >
                  {capabilityName(
                    cap,
                    allRows
                      .flatMap((row) => row.capabilities)
                      .find((entry) => entry.capability === cap)?.label,
                  )}
                </FilterToggle>
              ))}
              <div
                role="group"
                aria-label="Price filters (choose any)"
                className="ml-auto flex min-w-0 max-w-full flex-wrap gap-1 border-l pl-3 max-sm:ml-0 max-sm:border-l-0 max-sm:pl-0"
              >
                <button
                  type="button"
                  aria-pressed={showNoProviderCharge}
                  onClick={() => setShowNoProviderCharge((value) => !value)}
                  className="text-muted-foreground hover:bg-muted focus-visible:ring-ring aria-pressed:text-foreground h-8 max-sm:min-h-11 rounded-control px-3 text-meta underline-offset-4 transition outline-none focus-visible:ring-2 aria-pressed:underline"
                >
                  No provider charge
                </button>
                <button
                  type="button"
                  aria-pressed={showPaid}
                  onClick={() => setShowPaid((value) => !value)}
                  className="text-muted-foreground hover:bg-muted focus-visible:ring-ring aria-pressed:text-foreground h-8 max-sm:min-h-11 rounded-control px-3 text-meta underline-offset-4 transition outline-none focus-visible:ring-2 aria-pressed:underline"
                >
                  Paid
                </button>
                <button
                  type="button"
                  aria-pressed={showUnknownCost}
                  onClick={() => setShowUnknownCost((value) => !value)}
                  className="text-muted-foreground hover:bg-muted focus-visible:ring-ring aria-pressed:text-foreground h-8 max-sm:min-h-11 rounded-control px-3 text-meta underline-offset-4 transition outline-none focus-visible:ring-2 aria-pressed:underline"
                >
                  Cost not recorded
                </button>
              </div>
            </div>
          </section>
          {showLocalCostNote && (
            <p className="text-muted-foreground text-meta">{preferredTerms.localPricing}</p>
          )}
          {groupedAliases.length === 0 ? (
            <EmptyState
              icon={Search}
              title={
                modelRows.length
                  ? "No recommended names match these filters"
                  : "No models match these filters"
              }
              description={
                modelRows.length
                  ? "Show all models to use an exact model directly."
                  : "Clear the search or turn on another price filter."
              }
              action={
                modelRows.length ? (
                  <Button variant="outline" onClick={() => setTableOpen(true)}>
                    Show all models
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    onClick={() => {
                      setQuery("");
                      setCapability("all");
                      setShowNoProviderCharge(true);
                      setShowPaid(true);
                      setShowUnknownCost(true);
                    }}
                  >
                    Clear filters
                  </Button>
                )
              }
            />
          ) : (
            <div
              aria-label="Available model names"
              className="bg-card overflow-hidden rounded-panel border"
            >
              <Table aria-label="Available model names" className="max-sm:block">
                <TableHeader className="max-sm:hidden">
                  <TableRow>
                    <TableHead>Task and model name</TableHead>
                    <TableHead>Model</TableHead>
                    <TableHead>Model facts</TableHead>
                    {aliasHasCost && <TableHead numeric>Cost</TableHead>}
                    {aliasHasStatus && <TableHead>Status</TableHead>}
                    <TableHead><span className="sr-only">Actions</span></TableHead>
                  </TableRow>
                </TableHeader>
                {groupedAliases.map((group) => (
                  <CapabilityRow
                    key={group.capability}
                    rows={group.rows}
                    onDetail={selectDetail}
                    searching={Boolean(query.trim())}
                    showCost={aliasHasCost}
                    showStatus={aliasHasStatus}
                  />
                ))}
              </Table>
            </div>
          )}
          {(modelRows.length > 0 || (groupedAliases.length > 0 && otherModels.length > 0)) && <section className="space-y-3">
            <button
              type="button"
              className="hover:bg-muted/70 focus-visible:ring-ring flex w-full items-center justify-between rounded-panel border px-4 h-8 max-sm:min-h-11 text-left transition outline-none focus-visible:ring-2"
              onClick={() => setTableOpen((value) => !value)}
              aria-expanded={tableOpen}
            >
              <span>
                <span className="font-medium">All models</span>
              </span>
              <ChevronDown
                aria-hidden="true"
                className={cn("size-4 transition", tableOpen && "rotate-180")}
              />
            </button>
            {tableOpen && (modelRows.length === 0 ? (
              <EmptyState icon={Search} title="No models match these filters."
                description="Clear the filters to see more models."
                action={<Button variant="outline" onClick={() => { setQuery(""); setCapability("all"); setShowNoProviderCharge(true); setShowPaid(true); setShowUnknownCost(true); }}>Clear filters</Button>} />
            ) : (
              <div className="overflow-x-auto rounded-panel border">
                <Table aria-label="All models" className="max-sm:block">
                  <TableHeader className="max-sm:hidden">
                    <TableRow>
                      <TableHead>Task and model name</TableHead>
                      <TableHead>Task</TableHead>
                      <TableHead>Engine or provider</TableHead>
                      {modelHasCost && <TableHead numeric>Cost</TableHead>}
                      {modelHasStatus && <TableHead>Status</TableHead>}
                      {modelRows.length > 0 && <TableHead><span className="sr-only">Actions</span></TableHead>}
                    </TableRow>
                  </TableHeader>
                  <TableBody className="max-sm:block">
                    {modelRows.map((row) => (
                      <TableRow
                        key={row.id}
                        className={cn(
                          "cursor-pointer h-(--row-2)",
                          phoneRowClass,
                        )}
                        onClick={() => selectDetail(row)}
                      >
                        <TableCell className={cn("py-1! max-sm:contents", phoneCellClass)}>
                          <div className={phoneTaskClass}>
                            <span className="text-body truncate font-medium" title={rowTaskName(row)}>{rowTaskName(row)}</span>
                          </div>
                          <CopyModelName row={row} />
                        </TableCell>
                        <TableCell className="py-1! max-sm:hidden">
                          {titleFor(row) && <><span>{titleFor(row)}</span>{" · "}</>}
                          <span>{primaryCapabilityLabel(row)}</span>
                          <ModelFacts row={row} className="mt-1" />
                        </TableCell>
                        <TableCell className="py-1! max-sm:hidden">{summaryEngineLabel(row)}</TableCell>
                        {modelHasCost && <TableCell numeric title={costLabel(row)} className={cn("py-1!", phoneCellClass, phoneCostClass)}>{costLabel(row)}</TableCell>}
                        {modelHasStatus && <TableCell className={cn("py-1!", phoneCellClass, phoneStatusClass)}>
                          {hasSummaryStatus(row) && <Availability row={row} showReason={false} />}
                        </TableCell>}
                        <TableCell className={cn("py-1!", phoneCellClass, phoneActionsClass)}>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={(event) => {
                              event.stopPropagation();
                              selectDetail(row);
                            }}
                          >
                            Details
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ))}
          </section>}
        </>
      )}
      {detailNote && <div role="status"><p>This model is no longer in the catalog.</p><Button variant="ghost" onClick={() => setDetailNote(null)}>Dismiss</Button></div>}
      <DetailSheet
        row={detail}
        onOpenChange={(open) => !open && setDetailId(null)}
        viewerAdmin={viewer.admin}
      />
    </div>
  );
}
