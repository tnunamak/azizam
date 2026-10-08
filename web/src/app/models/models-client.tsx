"use client";

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
import { preferredTerms } from "@/lib/terms";
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
  chat: "Chat",
  image: "Images",
  video: "Video",
  music: "Music",
  sfx: "Sound effects",
  speech: "Speech",
  audio: "Audio",
  transcribe: "Transcription",
  decide: "Decisions",
  embed: "Embeddings",
  rerank: "Reranking",
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
      className="bg-muted text-muted-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-md"
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
  return (
    row.capability_title ??
    row.capabilities[0]?.label ??
    capabilityLabels[primaryCapability(row)]
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
            {inputs.join(", ")}
            <span aria-hidden="true">→</span>
            <span className="sr-only">to</span>
            {outputs.join(", ")}
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
  const capability = primaryCapabilityLabel(row);
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
      parts.length > 0 && parts.every((part) => part === normalize(capability))
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
    (row.locality === "remote" ? "Remote provider" : "Configured engine")
  );
}

function summaryEngineLabel(row: CatalogRow): string {
  if (row.locality === "local") return engineLabel(row);
  if (row.locality === "remote")
    return row.engine_label ?? row.provider ?? "Remote provider";
  return "Configured service";
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
    row.locality === "remote" ? "/ui/providers/" : "/ui/engines/";
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
    return { label: "Check status", href: "/ui/status/" };
  }
  return null;
}

function AvailabilityFacts({ row }: { row: CatalogRow }) {
  const enabled = enabledLabel(row);
  return (
    <div className="flex flex-wrap gap-2 text-xs">
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
  async function copyName() {
    try {
      await navigator.clipboard.writeText(row.id);
      setCopyError(false);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
      setCopyError(true);
    }
  }
  return (
    <div className="min-w-0">
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          void copyName();
        }}
        aria-label={`Copy model name ${row.id}`}
        className="bg-muted hover:bg-muted/80 focus-visible:ring-ring flex min-h-11 max-w-full min-w-0 items-center justify-between gap-2 rounded-md px-3 py-2 text-left font-mono text-xs transition outline-none focus-visible:ring-[3px]"
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
          <p role="alert" className="text-destructive text-sm">
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
    row.description,
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

function snippetsFor(row: CatalogRow, origin: string) {
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
  const isBinary = path.includes("/audio/speech");
  return [
    {
      language: "curl",
      code: isMultipart
        ? `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AZIZAM_API_KEY" \\\n  -F model=${shellSingleQuote(row.use.model)} \\\n  -F ${fileField}=@${fileName}${isImageUpload ? ` \\\n  -F prompt=${shellSingleQuote(String(body.prompt ?? "Edit this image"))}` : ""}`
        : `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AZIZAM_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d ${shellSingleQuote(json)}${isBinary ? " \\\n  --output speech.mp3" : ""}`,
    },
    {
      language: "Python",
      code: isMultipart
        ? `import os\nimport json\nimport requests\n\nwith open("${fileName}", "rb") as media:\n    response = requests.post(\n        ${JSON.stringify(url)},\n        headers={"Authorization": f"Bearer {os.environ['AZIZAM_API_KEY']}"},\n        data=json.loads(${JSON.stringify(JSON.stringify(body))}),\n        files={"${fileField}": media},\n        timeout=120,\n    )\nresponse.raise_for_status()\nprint(response.json())`
        : `import os\nimport json\nimport requests\n\nresponse = requests.post(\n    ${JSON.stringify(url)},\n    headers={\n        "Authorization": f"Bearer {os.environ['AZIZAM_API_KEY']}",\n        "Content-Type": "application/json",\n    },\n    json=json.loads(${JSON.stringify(json)}),\n    timeout=120,\n)\nresponse.raise_for_status()\n${isBinary ? 'open("speech.mp3", "wb").write(response.content)' : "print(response.json())"}`,
    },
    {
      language: "JavaScript",
      code: isMultipart
        ? `import { readFile } from "node:fs/promises";\n\nconst form = new FormData();\nconst fields = ${JSON.stringify(body)};\nfor (const [name, value] of Object.entries(fields)) form.set(name, String(value));\nform.set("${fileField}", new Blob([await readFile("${fileName}")]), "${fileName}");\n\nconst response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: { Authorization: \`Bearer \${process.env.AZIZAM_API_KEY}\` },\n  body: form,\n});\nif (!response.ok) throw new Error(await response.text());\nconsole.log(await response.json());`
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
  return (
    <StatusPill
      status={state}
      reason={
        showReason
          ? availabilityReason(state, row.availability.reason_code ?? state)
          : undefined
      }
      summary={!showReason || state === "unknown" || state === "not_measured"}
    />
  );
}

function AliasOptionRow({
  row,
  onDetail,
}: {
  row: CatalogRow;
  onDetail: (row: CatalogRow) => void;
}) {
  return (
    <article
      className="hover:bg-muted/40 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 sm:flex-nowrap sm:pl-14"
      onClick={() => onDetail(row)}
    >
      <div className="min-w-0 flex-1 basis-40">
        <h3 className="min-h-5 truncate text-sm font-medium">
          {titleFor(row) ?? ""}
        </h3>
        <p className="text-muted-foreground line-clamp-1 min-h-4 text-xs">
          {descriptionFor(row)}
        </p>
        <p className="text-muted-foreground min-h-4 text-xs">
          {costLabel(row) ?? ""}
        </p>
        <ModelFacts row={row} className="mt-1" />
      </div>
      <CopyModelName row={row} />
      {availabilityState(row) !== "ready" && (
        <Availability row={row} showReason={false} />
      )}
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
    </article>
  );
}

function CapabilityRow({
  rows,
  onDetail,
  searching,
}: {
  rows: CatalogRow[];
  onDetail: (row: CatalogRow) => void;
  searching: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  // An unmarked catalog has a neutral first option, never an inferred recommendation.
  const primary = rows.find((row) => row.recommended) ?? rows[0];
  const alternatives = rows.filter((row) => row !== primary);
  const open = expanded || searching;
  const alternativesId = `model-alternatives-${primaryCapability(primary)}`;
  return (
    <section className="min-w-0 border-b last:border-b-0">
      <article
        className="hover:bg-muted/40 grid min-h-16 grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-3 py-2 xl:grid-cols-[2rem_8rem_minmax(0,1fr)_9rem_7rem_5rem_2.75rem] xl:px-4"
        onClick={() => onDetail(primary)}
      >
        <CatalogIcon row={primary} />
        <h2 className="text-sm font-semibold">
          {primaryCapabilityLabel(primary)}
        </h2>
        <div className="col-start-2 row-start-2 min-w-0 space-y-1 xl:col-start-auto xl:row-start-auto">
          <h3 className="truncate text-sm">{titleFor(primary)}</h3>
          <ModelFacts row={primary} />
        </div>
        <div className="col-start-2 row-start-3 flex min-w-0 flex-wrap items-center gap-2 xl:col-start-auto xl:row-start-auto">
          <CopyModelName row={primary} />
          {costLabel(primary) && (
            <span className="text-muted-foreground text-xs">
              {costLabel(primary)}
            </span>
          )}
        </div>
        <div className="col-start-3 row-start-2 xl:col-start-auto xl:row-start-auto">
          {availabilityState(primary) !== "ready" && (
            <Availability row={primary} showReason={false} />
          )}
        </div>
        <div className="col-start-3 row-start-3 xl:col-start-auto xl:row-start-auto">
          {alternatives.length > 0 && (
            <button
              type="button"
              aria-label={`${alternatives.length} more ${primaryCapabilityLabel(primary)} models`}
              aria-expanded={open}
              aria-controls={alternativesId}
              className="text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex min-h-11 items-center gap-1 rounded px-2 text-xs whitespace-nowrap outline-none focus-visible:ring-[3px]"
              onClick={(event) => {
                event.stopPropagation();
                setExpanded((value) => !value);
              }}
            >
              {alternatives.length} more
              <ChevronDown
                aria-hidden="true"
                className={cn("size-3 transition", open && "rotate-180")}
              />
            </button>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="col-start-3 row-start-1 xl:col-start-auto xl:row-start-auto"
          onClick={(event) => {
            event.stopPropagation();
            onDetail(primary);
          }}
        >
          <span className="sr-only">Details</span>
          <ChevronRight aria-hidden="true" />
        </Button>
      </article>
      {open && alternatives.length > 0 && (
        <div id={alternativesId} className="bg-muted/20 divide-y border-t">
          {alternatives.map((row) => (
            <AliasOptionRow key={row.id} row={row} onDetail={onDetail} />
          ))}
        </div>
      )}
    </section>
  );
}

function LoadingCatalog() {
  return (
    <div aria-label="Loading models" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-11 w-56" />
        <Skeleton className="h-11 w-28" />
        <Skeleton className="h-11 w-28" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-64 rounded-xl" />
        ))}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-muted-foreground text-xs">{label}</div>
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
              <div className="grid gap-3 text-sm sm:grid-cols-2">
                <Fact
                  label={row.alias ? "Model name" : "Exact model"}
                  value={row.id}
                />
                {Object.keys(row.resolves_to).length > 0 && (
                  <Fact
                    label="Resolves to"
                    value={Object.entries(row.resolves_to)
                      .map(([op, target]) => `${op}: ${target}`)
                      .join(", ")}
                  />
                )}
                {engineLabel(row) !== titleFor(row) && (
                  <Fact label="Engine or provider" value={engineLabel(row)} />
                )}
                {locality && <Fact label="Location" value={locality} />}
                {cost && <Fact label="Cost" value={cost} />}
                {row.technical_description && (
                  <Fact
                    label="Technical description"
                    value={row.technical_description}
                  />
                )}
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
              <div>
                <h3 className="font-medium">Availability</h3>
                <div className="mt-3 space-y-3 rounded-lg border p-3">
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
                  <p className="text-muted-foreground mt-2 text-sm">{row.id}</p>
                )}
              </div>
              <div>
                <h3 className="font-medium">Inputs and outputs</h3>
                {(row.io.input.length > 0 || row.io.output.length > 0) && (
                  <dl className="text-muted-foreground mt-2 space-y-1 text-sm">
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
              </div>
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
                  <ul className="mt-3 space-y-2 text-sm">
                    {row.recent_receipts.slice(0, 5).map((receipt, index) => (
                      <li
                        key={readString(receipt.request_id) ?? index}
                        className="rounded-lg border p-3"
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
                            className="mt-2 block min-h-11 content-center underline underline-offset-4"
                            href={`/r/${encodeURIComponent(readString(receipt.request_id) ?? "")}`}
                          >
                            View receipt
                          </a>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground mt-2 text-sm">
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
  const [tableOpen, setTableOpen] = useState(false);
  const [detail, setDetail] = useState<CatalogRow | null>(null);
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
    sectionRows.some((row) => primaryCapability(row) === cap),
  );
  const passesFilters = (row: CatalogRow) => {
    if (!itemMatches(row, query)) return false;
    if (capability !== "all" && !hasCapability(row, capability)) {
      return false;
    }
    if (hasNoProviderCharge(row) && !showNoProviderCharge) return false;
    if (!hasNoProviderCharge(row) && row.cost.kind === "paid" && !showPaid)
      return false;
    return true;
  };
  const modelRows = (catalogData?.models ?? []).filter(passesFilters);
  const groupedAliases = productCapabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: sectionRows.filter(
        (row) => passesFilters(row) && primaryCapability(row) === cap,
      ),
    }))
    .filter((group) => group.rows.length > 0);

  return (
    <div className="space-y-4">
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
              <a href="/ui/home/">Review setup</a>
            </Button>
          }
        />
      ) : (
        <>
          <section aria-label="Model filters" className="space-y-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
              <label className="relative min-w-0 flex-1">
                <span className="sr-only">Search models</span>
                <Search
                  aria-hidden="true"
                  className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
                />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={`Search ${preferredTerms.requestSelector.toLowerCase()}s or providers`}
                  className="pl-9"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2 pb-1">
              <FilterToggle
                className="px-3 text-xs [&>svg]:hidden"
                pressed={capability === "all"}
                onClick={() => setCapability("all")}
              >
                All
              </FilterToggle>
              {availableCapabilities.map((cap) => (
                <FilterToggle
                  key={cap}
                  className="px-3 text-xs [&>svg]:hidden"
                  pressed={capability === cap}
                  onClick={() => setCapability(cap)}
                >
                  {allRows
                    .flatMap((row) => row.capabilities)
                    .find((entry) => entry.capability === cap)?.label ??
                    capabilityLabels[cap]}
                </FilterToggle>
              ))}
              <div
                role="group"
                aria-label="Price filters (choose any)"
                className="ml-auto flex shrink-0 gap-1 border-l pl-3"
              >
                <button
                  type="button"
                  aria-pressed={showNoProviderCharge}
                  onClick={() => setShowNoProviderCharge((value) => !value)}
                  className="text-muted-foreground hover:bg-muted focus-visible:ring-ring aria-pressed:text-foreground min-h-11 rounded-md px-3 text-xs underline-offset-4 transition outline-none focus-visible:ring-[3px] aria-pressed:underline"
                >
                  No provider charge
                </button>
                <button
                  type="button"
                  aria-pressed={showPaid}
                  onClick={() => setShowPaid((value) => !value)}
                  className="text-muted-foreground hover:bg-muted focus-visible:ring-ring aria-pressed:text-foreground min-h-11 rounded-md px-3 text-xs underline-offset-4 transition outline-none focus-visible:ring-[3px] aria-pressed:underline"
                >
                  Paid
                </button>
              </div>
            </div>
          </section>
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
                    }}
                  >
                    Clear filters
                  </Button>
                )
              }
            />
          ) : (
            <div
              aria-label="Available model aliases"
              className="bg-card overflow-hidden rounded-lg border"
            >
              {groupedAliases.map((group) => (
                <CapabilityRow
                  key={group.capability}
                  rows={group.rows}
                  onDetail={setDetail}
                  searching={Boolean(query.trim())}
                />
              ))}
            </div>
          )}
          <section className="space-y-3">
            <button
              type="button"
              className="hover:bg-muted/70 focus-visible:ring-ring flex w-full items-center justify-between rounded-lg border px-4 py-3 text-left transition outline-none focus-visible:ring-[3px]"
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
            {tableOpen && (
              <div className="overflow-x-auto rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead>Capability</TableHead>
                      <TableHead>Engine or provider</TableHead>
                      <TableHead>Cost</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Use</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {modelRows.map((row) => (
                      <TableRow
                        key={row.id}
                        className="cursor-pointer"
                        onClick={() => setDetail(row)}
                      >
                        <TableCell className="font-medium">
                          {titleFor(row) && (
                            <span className="mb-1 block">{titleFor(row)}</span>
                          )}
                          <CopyModelName row={row} />
                        </TableCell>
                        <TableCell>
                          {primaryCapabilityLabel(row)}
                          <ModelFacts row={row} className="mt-1" />
                        </TableCell>
                        <TableCell>{summaryEngineLabel(row)}</TableCell>
                        <TableCell>{costLabel(row)}</TableCell>
                        <TableCell>
                          <Availability row={row} showReason={false} />
                        </TableCell>
                        <TableCell>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDetail(row);
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
            )}
          </section>
        </>
      )}
      <DetailSheet
        row={detail}
        onOpenChange={(open) => !open && setDetail(null)}
        viewerAdmin={viewer.admin}
      />
    </div>
  );
}
