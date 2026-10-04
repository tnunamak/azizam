"use client";

import { useMemo, useState } from "react";
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
import { productApi, type CatalogRow } from "@/lib/product-api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useViewer } from "@/components/shell/session";
import { FilterToggle } from "@/components/product/filter-toggle";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
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
  RelativeTime,
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

type CatalogCapabilityEntry = CatalogRow["capabilities"][number];

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
  transcribe: "Transcribe",
  decide: "Decide",
  embed: "Embed",
  rerank: "Rerank",
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

function entryCapability(entry: CatalogCapabilityEntry): ProductCapability {
  return (entry.capability as ProductCapability | undefined) ?? "chat";
}

function primaryCapability(row: CatalogRow): ProductCapability {
  const entry = row.capabilities[0] as CatalogCapabilityEntry | undefined;
  return entry ? entryCapability(entry) : "chat";
}

function primaryCapabilityLabel(row: CatalogRow): string {
  const entry = row.capabilities[0] as CatalogCapabilityEntry | undefined;
  return (
    row.capability_title ??
    entry?.label ??
    capabilityLabels[primaryCapability(row)]
  );
}

function hasCapability(
  row: CatalogRow,
  capability: ProductCapability,
): boolean {
  return row.capabilities.some(
    (entry) => entryCapability(entry as CatalogCapabilityEntry) === capability,
  );
}

function descriptionFor(row: CatalogRow): string {
  return row.description ?? "";
}

function titleFor(row: CatalogRow): string {
  return row.title ?? row.display_name;
}

function hasNoProviderCharge(row: CatalogRow): boolean {
  return row.locality === "local" || row.cost.kind === "free";
}

function costLabel(row: CatalogRow): string {
  if (row.locality === "local") return "Local · no provider charge";
  if (hasNoProviderCharge(row)) return "No provider charge";
  if (row.cost.kind === "paid") {
    return row.cost.price === null
      ? "Paid · Price not provided"
      : `Paid · ${String(row.cost.price)}`;
  }
  return "Price not provided";
}

function localityLabel(row: CatalogRow): string {
  if (row.locality === "local") return "Local";
  if (row.locality === "remote") return "Remote";
  return "Placement not configured";
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

function timingLabel(row: CatalogRow): string {
  const value = row.timing.typical_seconds;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "Not measured yet";
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
        : "Source not provided";
  return `${duration} · ${source}`;
}

function timingSummaryLabel(row: CatalogRow): string | null {
  const value = row.timing.typical_seconds;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  return timingLabel(row);
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

function evidenceLabel(row: CatalogRow): string {
  const source = row.availability.evidence?.source;
  if (row.availability.measured) {
    if (source === "request") return "Observed from request";
    if (source === "health" || source === "status")
      return "Observed from health";
    if (source === "registry") return "From configuration";
    return "Observed";
  }
  return "Not observed yet";
}

function observedAt(row: CatalogRow): number | null {
  const value = row.availability.evidence?.observed_at_s;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function configurationLabel(row: CatalogRow): string {
  if (row.availability.configured === false) return "Catalog only";
  if (row.availability.configured === true) return "Configured";
  return "Configuration not reported";
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
      <Badge variant="outline">{configurationLabel(row)}</Badge>
      <Badge variant="outline">{evidenceLabel(row)}</Badge>
      {enabled && <Badge variant="outline">{enabled}</Badge>}
    </div>
  );
}

function ObservedAt({ row }: { row: CatalogRow }) {
  const at = observedAt(row);
  if (at === null) {
    return (
      <span className="text-muted-foreground text-xs">
        Observation time not reported
      </span>
    );
  }
  return (
    <span className="text-muted-foreground text-xs">
      Observed: <RelativeTime ts={at} />
    </span>
  );
}

function TableUseCell({
  row,
  viewerAdmin,
  onDetail,
}: {
  row: CatalogRow;
  viewerAdmin: boolean;
  onDetail: (row: CatalogRow) => void;
}) {
  const action = availabilityAction(row, viewerAdmin);
  if (action) {
    return (
      <Button size="sm" asChild>
        <a href={action.href} onClick={(event) => event.stopPropagation()}>
          {action.label}
        </a>
      </Button>
    );
  }
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={(event) => {
        event.stopPropagation();
        onDetail(row);
      }}
    >
      Details
    </Button>
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
        ? `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AI_GATEWAY_API_KEY" \\\n  -F model=${shellSingleQuote(row.use.model)} \\\n  -F ${fileField}=@${fileName}${isImageUpload ? ` \\\n  -F prompt=${shellSingleQuote(String(body.prompt ?? "Edit this image"))}` : ""}`
        : `curl ${shellSingleQuote(url)} \\\n  -H "Authorization: Bearer $AI_GATEWAY_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d ${shellSingleQuote(json)}${isBinary ? " \\\n  --output speech.mp3" : ""}`,
    },
    {
      language: "Python",
      code: isMultipart
        ? `import os\nimport json\nimport requests\n\nwith open("${fileName}", "rb") as media:\n    response = requests.post(\n        ${JSON.stringify(url)},\n        headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"},\n        data=json.loads(${JSON.stringify(JSON.stringify(body))}),\n        files={"${fileField}": media},\n        timeout=120,\n    )\nresponse.raise_for_status()\nprint(response.json())`
        : `import os\nimport json\nimport requests\n\nresponse = requests.post(\n    ${JSON.stringify(url)},\n    headers={\n        "Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}",\n        "Content-Type": "application/json",\n    },\n    json=json.loads(${JSON.stringify(json)}),\n    timeout=120,\n)\nresponse.raise_for_status()\n${isBinary ? 'open("speech.mp3", "wb").write(response.content)' : "print(response.json())"}`,
    },
    {
      language: "JavaScript",
      code: isMultipart
        ? `import { readFile } from "node:fs/promises";\n\nconst form = new FormData();\nconst fields = ${JSON.stringify(body)};\nfor (const [name, value] of Object.entries(fields)) form.set(name, String(value));\nform.set("${fileField}", new Blob([await readFile("${fileName}")]), "${fileName}");\n\nconst response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: { Authorization: \`Bearer \${process.env.AI_GATEWAY_API_KEY}\` },\n  body: form,\n});\nif (!response.ok) throw new Error(await response.text());\nconsole.log(await response.json());`
        : `${isBinary ? 'import { writeFile } from "node:fs/promises";\n\n' : ""}const response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: {\n    Authorization: \`Bearer \${process.env.AI_GATEWAY_API_KEY}\`,\n    "Content-Type": "application/json",\n  },\n  body: JSON.stringify(${JSON.stringify(body, null, 2)}),\n});\nif (!response.ok) throw new Error(await response.text());\n${isBinary ? 'await writeFile("speech.mp3", Buffer.from(await response.arrayBuffer()));' : "console.log(await response.json());"}`,
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
      reason={showReason ? row.availability.reason : undefined}
    />
  );
}

function AliasCard({
  row,
  onDetail,
  viewerAdmin,
}: {
  row: CatalogRow;
  onDetail: (row: CatalogRow) => void;
  viewerAdmin: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const action = availabilityAction(row, viewerAdmin);
  const timingSummary = timingSummaryLabel(row);
  async function copyAlias() {
    await navigator.clipboard.writeText(row.id);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }
  return (
    <article className="bg-card text-card-foreground flex min-w-0 flex-col gap-5 rounded-xl border p-5">
      <div className="flex min-w-0 items-start gap-3">
        <CatalogIcon row={row} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-semibold">{titleFor(row)}</h3>
          <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">
            {descriptionFor(row)}
          </p>
        </div>
      </div>
      <button
        type="button"
        onClick={copyAlias}
        aria-label={`Copy alias ${row.id}`}
        className="bg-muted hover:bg-muted/80 focus-visible:ring-ring flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-md px-3 py-2 text-left font-mono text-xs transition outline-none focus-visible:ring-[3px]"
      >
        <span className="truncate">{row.id}</span>
        {copied ? (
          <Check aria-label="Alias copied" className="size-4 shrink-0" />
        ) : (
          <Copy aria-hidden="true" className="size-4 shrink-0" />
        )}
      </button>
      <div
        data-slot="model-metadata"
        className="text-muted-foreground flex min-w-0 flex-wrap items-center gap-1 text-xs"
      >
        <span className="shrink-0">{localityLabel(row)}</span>
        <span
          className="min-w-20 flex-1 truncate"
          title={summaryEngineLabel(row)}
        >
          <span aria-hidden="true">· </span>
          {summaryEngineLabel(row)}
        </span>
      </div>
      <div className="text-muted-foreground flex min-w-0 flex-wrap items-center gap-2 text-xs">
        <Badge variant="outline" className="max-w-full text-xs">
          <span className="whitespace-normal">{costLabel(row)}</span>
        </Badge>
        {timingSummary && (
          <span
            className="inline-flex shrink-0 items-center gap-1"
            title={
              row.timing.source === "receipts"
                ? "Measured time from successful requests"
                : "Typical time"
            }
          >
            <span>{timingSummary}</span>
          </span>
        )}
      </div>
      <Availability row={row} />
      <div className="mt-auto flex flex-wrap gap-2">
        {action ? (
          <Button size="sm" asChild>
            <a href={action.href}>{action.label}</a>
          </Button>
        ) : (
          <Popover>
            <PopoverTrigger asChild>
              <Button size="sm">Use</Button>
            </PopoverTrigger>
            <PopoverContent
              align="start"
              className="w-[min(92vw,560px)] p-0"
              aria-label={`Use ${row.id}`}
            >
              <div className="border-b p-4">
                <h4 className="font-medium">Use {row.id}</h4>
                <p className="text-muted-foreground mt-1 text-sm">
                  Copy the alias or a starter request.
                </p>
              </div>
              <div className="p-4">
                <CodeBlock
                  snippets={snippetsFor(row, window.location.origin)}
                />
              </div>
            </PopoverContent>
          </Popover>
        )}
        <Button variant="outline" size="sm" onClick={() => onDetail(row)}>
          Details
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
    </article>
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
  return (
    <Sheet open={!!row} onOpenChange={onOpenChange}>
      <SheetContent className="w-[92vw] overflow-y-auto sm:max-w-xl">
        {row && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>{titleFor(row)}</SheetTitle>
              <SheetDescription>{descriptionFor(row)}</SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <div className="grid gap-3 text-sm sm:grid-cols-2">
                <Fact label={row.alias ? "Alias" : "Model"} value={row.id} />
                <Fact
                  label="Resolves to"
                  value={
                    Object.keys(row.resolves_to).length
                      ? Object.entries(row.resolves_to)
                          .map(([op, target]) => `${op}: ${target}`)
                          .join(", ")
                      : "Not reported"
                  }
                />
                <Fact label="Engine or provider" value={engineLabel(row)} />
                <Fact label="Location" value={localityLabel(row)} />
                <Fact label="Cost" value={costLabel(row)} />
                {row.technical_description && (
                  <Fact
                    label="Technical description"
                    value={row.technical_description}
                  />
                )}
                <Fact
                  label={
                    row.timing.source === "receipts"
                      ? "Measured time"
                      : "Typical time"
                  }
                  value={timingLabel(row)}
                />
              </div>
              <div>
                <h3 className="font-medium">Availability</h3>
                <div className="mt-3 space-y-3 rounded-lg border p-3">
                  <Availability row={row} />
                  <AvailabilityFacts row={row} />
                  <ObservedAt row={row} />
                  {action && (
                    <Button size="sm" asChild>
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
                <p className="text-muted-foreground mt-2 text-sm">
                  Inputs:{" "}
                  {row.io.input.length
                    ? row.io.input.join(", ")
                    : "Not reported"}
                  . Outputs:{" "}
                  {row.io.output.length
                    ? row.io.output.join(", ")
                    : "Not reported"}
                  .
                </p>
              </div>
              <div>
                <h3 className="font-medium">Example request</h3>
                <div className="mt-3">
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
                        {readString(receipt.status) ?? "Recorded request"} ·{" "}
                        {readNumber(receipt.latency_ms) !== null
                          ? `${readNumber(receipt.latency_ms)} ms`
                          : "Duration not measured"}
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
  const [moreChatOpen, setMoreChatOpen] = useState(false);
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
  const availableCapabilities = productCapabilityOrder.filter((cap) =>
    allRows.some((row) => hasCapability(row, cap)),
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
  const aliasRows = (catalogData?.aliases ?? []).filter(passesFilters);
  const modelRows = (catalogData?.models ?? []).filter(
    (row) => passesFilters(row) && !row.secondary_profile,
  );
  const secondaryChatRows = allRows.filter(
    (row) => passesFilters(row) && row.secondary_profile,
  );
  const primaryAliasRows = aliasRows.filter((row) => !row.secondary_profile);
  const groupedAliases = productCapabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: primaryAliasRows.filter((row) => primaryCapability(row) === cap),
    }))
    .filter((group) => group.rows.length > 0);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Models"
        answer="What can I use, and which one should I pick?"
      />
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
          description="Models appear here after an owner connects a local engine or remote provider that your access plan can use."
          action={
            <Button asChild>
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
                  placeholder="Search models, aliases or providers"
                  className="pl-9"
                />
              </label>
              <div
                role="group"
                aria-label="Price filters (choose any)"
                className="flex flex-wrap gap-2"
              >
                <FilterToggle
                  pressed={showNoProviderCharge}
                  onClick={() => setShowNoProviderCharge((value) => !value)}
                >
                  No provider charge
                </FilterToggle>
                <FilterToggle
                  pressed={showPaid}
                  onClick={() => setShowPaid((value) => !value)}
                >
                  Paid
                </FilterToggle>
              </div>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              <FilterToggle
                pressed={capability === "all"}
                onClick={() => setCapability("all")}
              >
                All
              </FilterToggle>
              {availableCapabilities.map((cap) => (
                <FilterToggle
                  key={cap}
                  pressed={capability === cap}
                  onClick={() => setCapability(cap)}
                >
                  {allRows
                    .flatMap((row) => row.capabilities)
                    .find((entry) => entryCapability(entry) === cap)?.label ??
                    capabilityLabels[cap]}
                </FilterToggle>
              ))}
            </div>
          </section>
          {groupedAliases.length === 0 && secondaryChatRows.length === 0 ? (
            <EmptyState
              icon={Search}
              title={
                modelRows.length
                  ? "No stable aliases match these filters"
                  : "No models match these filters"
              }
              description={
                modelRows.length
                  ? "Expand All models below to use a model directly."
                  : "Clear the search or turn on another price filter."
              }
              action={
                modelRows.length ? (
                  <Button onClick={() => setTableOpen(true)}>
                    Show all models
                  </Button>
                ) : (
                  <Button
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
            <div className="space-y-4">
              <p className="text-muted-foreground text-sm">
                Stable aliases stay the same when the backing model changes.
                This catalog is scoped to models your access plan allows.
              </p>
              <div
                aria-label="Available model aliases"
                className={cn(
                  "grid gap-6",
                  primaryAliasRows.length > 1 && "sm:grid-cols-2",
                  primaryAliasRows.length > 2 && "lg:grid-cols-3",
                )}
              >
                {groupedAliases.map((group) => (
                  <section
                    key={group.capability}
                    className={cn(
                      "min-w-0 space-y-4",
                      group.rows.length > 1 && "sm:col-span-2",
                      group.rows.length > 2 && "lg:col-span-3",
                    )}
                  >
                    <div className="flex items-center gap-3">
                      <CatalogIcon row={group.rows[0]} />
                      <h2 className="text-lg font-semibold">
                        {primaryCapabilityLabel(group.rows[0])}
                      </h2>
                    </div>
                    <div
                      className={cn(
                        "grid gap-4",
                        group.rows.length > 1 && "sm:grid-cols-2",
                        group.rows.length > 2 && "lg:grid-cols-3",
                      )}
                    >
                      {group.rows.map((row) => (
                        <AliasCard
                          key={row.id}
                          row={row}
                          onDetail={setDetail}
                          viewerAdmin={viewer.admin}
                        />
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            </div>
          )}
          {secondaryChatRows.length > 0 && (
            <section className="space-y-3">
              <Button
                variant="outline"
                className="w-full justify-between"
                aria-expanded={moreChatOpen || Boolean(query.trim())}
                aria-controls="more-chat-profiles"
                onClick={() => setMoreChatOpen((value) => !value)}
              >
                <span>More chat profiles · {secondaryChatRows.length}</span>
                <ChevronDown
                  aria-hidden="true"
                  className={cn(
                    "size-4 transition",
                    (moreChatOpen || Boolean(query.trim())) && "rotate-180",
                  )}
                />
              </Button>
              {(moreChatOpen || Boolean(query.trim())) && (
                <div
                  id="more-chat-profiles"
                  className="divide-y rounded-lg border px-4"
                >
                  {secondaryChatRows.map((row) => (
                    <div
                      key={row.id}
                      className="flex min-w-0 flex-wrap items-center gap-3 py-4"
                    >
                      <div className="min-w-0 flex-1 basis-48">
                        <h3 className="font-medium">{titleFor(row)}</h3>
                        <p className="text-muted-foreground mt-1 text-sm">
                          {descriptionFor(row)}
                        </p>
                      </div>
                      <code className="max-w-full text-xs break-all">
                        {row.id}
                      </code>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setDetail(row)}
                      >
                        Details
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </section>
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
                <span className="text-muted-foreground ml-2 text-sm">
                  {modelRows.length} shown
                </span>
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
                          <Button
                            variant="ghost"
                            className="h-auto min-h-11 justify-start text-left whitespace-normal"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDetail(row);
                            }}
                          >
                            <span className="space-y-1">
                              <span className="block">{titleFor(row)}</span>
                              <code className="text-muted-foreground block text-xs font-normal">
                                {row.id}
                              </code>
                            </span>
                          </Button>
                        </TableCell>
                        <TableCell>{primaryCapabilityLabel(row)}</TableCell>
                        <TableCell>{summaryEngineLabel(row)}</TableCell>
                        <TableCell>{costLabel(row)}</TableCell>
                        <TableCell>
                          <Availability row={row} showReason={false} />
                        </TableCell>
                        <TableCell>
                          <TableUseCell
                            row={row}
                            viewerAdmin={viewer.admin}
                            onDetail={setDetail}
                          />
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
