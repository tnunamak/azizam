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
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
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
  CapabilityIcon,
  type Capability,
} from "@/components/product/capability-icon";
import {
  CodeBlock,
  EmptyState,
  ErrorState,
  PageHeader,
} from "@/components/product";

type AvailabilityState =
  | "ready"
  | "starting"
  | "busy"
  | "unavailable"
  | "not measured yet";
type CostKind = "free" | "paid" | "unknown";
type Locality = "local" | "remote" | "unknown";

interface CatalogCapability {
  intent: string;
  label: string;
  input_modalities: string[];
  output_modalities: string[];
  streaming?: boolean;
  available?: boolean;
}

interface CatalogRow {
  id: string;
  object: "model_catalog.alias" | "model_catalog.model";
  display_name: string;
  name?: string;
  alias: boolean;
  operations: string[];
  resolves_to: Record<string, string>;
  description: string | null;
  capabilities: CatalogCapability[];
  provider: string | null;
  locality: Locality;
  cost: { kind: CostKind; price: string | number | null };
  timing: {
    typical_seconds: number | null;
    source: "configured" | "receipts" | string | null;
    sample_count?: number;
  };
  availability: {
    available: boolean;
    reason: string;
    measured: boolean;
    status?: string;
  };
  io: { input: string[]; output: string[] };
  examples: Array<{ kind: string; body: Record<string, unknown> }>;
  recent_receipts: Array<{
    request_id?: string;
    href?: string;
    status?: string;
    latency_ms?: number | null;
  }>;
  use: {
    model: string;
    curl?: string;
    python?: { path: string; model: string };
    javascript?: { path: string; model: string };
  };
}

interface Catalog {
  schema_version: 1;
  object: "models.catalog";
  aliases: CatalogRow[];
  models: CatalogRow[];
  counts?: { aliases: number; models: number; capabilities: number };
}

const capabilityOrder: Capability[] = [
  "chat",
  "image",
  "video",
  "voice",
  "transcribe",
  "decide",
  "embed",
];

const capabilityLabels: Record<Capability, string> = {
  chat: "Chat",
  image: "Images",
  video: "Video",
  voice: "Speech",
  transcribe: "Transcribe",
  decide: "Decide",
  embed: "Embed",
};

async function fetchCatalog(): Promise<Catalog> {
  const response = await fetch("/me/api/models/catalog", {
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error("Models catalog could not be loaded. Try again.");
  }
  const payload = (await response.json()) as unknown;
  if (!isCatalog(payload)) {
    throw new Error("Models catalog response is not in the expected shape.");
  }
  return payload;
}

function isCatalog(value: unknown): value is Catalog {
  if (!value || typeof value !== "object") return false;
  const maybe = value as Partial<Catalog>;
  return (
    maybe.schema_version === 1 &&
    maybe.object === "models.catalog" &&
    Array.isArray(maybe.aliases) &&
    Array.isArray(maybe.models)
  );
}

function capabilityFromIntent(intent: string | undefined): Capability {
  if (!intent) return "chat";
  if (intent.startsWith("image.")) return "image";
  if (intent.startsWith("video.")) return "video";
  if (intent === "speech.synthesis" || intent === "audio.generate") {
    return "voice";
  }
  if (intent.startsWith("speech.transcription")) return "transcribe";
  if (intent === "text.embeddings") return "embed";
  if (intent.includes("decision") || intent.includes("systemone")) {
    return "decide";
  }
  return "chat";
}

function primaryCapability(row: CatalogRow): Capability {
  return capabilityFromIntent(row.capabilities[0]?.intent);
}

function hasCapability(row: CatalogRow, capability: Capability): boolean {
  return row.capabilities.some(
    (entry) => capabilityFromIntent(entry.intent) === capability,
  );
}

function descriptionFor(row: CatalogRow): string {
  if (row.description) return row.description;
  const label =
    row.capabilities[0]?.label ?? capabilityLabels[primaryCapability(row)];
  return `${label} model visible to your access plan.`;
}

function availabilityLabel(value: AvailabilityState): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function costLabel(row: CatalogRow): string {
  if (row.cost.kind === "free")
    return row.locality === "local" ? "Free (your hardware)" : "Free";
  if (row.cost.kind === "paid") {
    return row.cost.price === null
      ? "Paid · Not measured yet"
      : `Paid · ${String(row.cost.price)}`;
  }
  return "Not measured yet";
}

function localityLabel(row: CatalogRow): string {
  if (row.locality === "local") return "Local";
  if (row.locality === "remote") return "Remote";
  return "Location not measured yet";
}

function timingLabel(row: CatalogRow): string {
  const value = row.timing.typical_seconds;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "Not measured yet";
  }
  if (value < 1) return `~${Math.round(value * 1000)} ms`;
  return `~${value.toFixed(value >= 10 ? 0 : 1)} s`;
}

function availabilityState(row: CatalogRow): AvailabilityState {
  if (!row.availability.measured) return "not measured yet";
  if (row.availability.status) {
    const status = row.availability.status.toLowerCase();
    if (["ready", "starting", "busy", "unavailable"].includes(status)) {
      return status as AvailabilityState;
    }
  }
  return row.availability.available ? "ready" : "unavailable";
}

function itemMatches(row: CatalogRow, query: string): boolean {
  if (!query) return true;
  const haystack = [
    row.id,
    row.name,
    row.display_name,
    row.description,
    row.provider,
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
  const path = row.use.python?.path ?? row.use.javascript?.path;
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

function availabilityClass(state: AvailabilityState) {
  if (state === "ready") return "bg-success";
  if (state === "starting") return "bg-info";
  if (state === "busy") return "bg-warning";
  return "bg-muted-foreground";
}

function Availability({ row }: { row: CatalogRow }) {
  const state = availabilityState(row);
  return (
    <div className="flex min-w-0 items-start gap-2 text-sm">
      <span
        aria-hidden="true"
        className={cn(
          "mt-1.5 size-2 shrink-0 rounded-full",
          availabilityClass(state),
        )}
      />
      <div className="min-w-0">
        <div className="font-medium">{availabilityLabel(state)}</div>
        <p className="text-muted-foreground line-clamp-2 text-xs">
          {row.availability.reason}
        </p>
      </div>
    </div>
  );
}

function AliasCard({
  row,
  onDetail,
}: {
  row: CatalogRow;
  onDetail: (row: CatalogRow) => void;
}) {
  const [copied, setCopied] = useState(false);
  async function copyAlias() {
    await navigator.clipboard.writeText(row.id);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }
  return (
    <article className="bg-card text-card-foreground flex min-w-0 flex-col gap-5 rounded-xl border p-5">
      <div className="flex min-w-0 items-start gap-3">
        <CapabilityIcon capability={primaryCapability(row)} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-semibold">
            {row.name ?? row.display_name}
          </h3>
          <p className="text-muted-foreground mt-1 line-clamp-2 text-sm">
            {descriptionFor(row)}
          </p>
        </div>
      </div>
      <button
        type="button"
        onClick={copyAlias}
        className="bg-muted hover:bg-muted/80 focus-visible:ring-ring flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-md px-3 py-2 text-left font-mono text-xs transition outline-none focus-visible:ring-[3px]"
      >
        <span className="truncate">{row.id}</span>
        {copied ? (
          <Check aria-label="Alias copied" className="size-4 shrink-0" />
        ) : (
          <Copy aria-hidden="true" className="size-4 shrink-0" />
        )}
      </button>
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">
          {localityLabel(row)} · {row.provider ?? "Provider unknown"}
        </Badge>
        <Badge variant={row.cost.kind === "paid" ? "outline" : "secondary"}>
          {costLabel(row)}
        </Badge>
        <Badge variant="outline">{timingLabel(row)}</Badge>
      </div>
      <Availability row={row} />
      <div className="mt-auto flex flex-wrap gap-2">
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
              <CodeBlock snippets={snippetsFor(row, window.location.origin)} />
            </div>
          </PopoverContent>
        </Popover>
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
      <div className="grid gap-4 lg:grid-cols-2">
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
  aliases,
  onOpenChange,
}: {
  row: CatalogRow | null;
  aliases: CatalogRow[];
  onOpenChange: (open: boolean) => void;
}) {
  const aliasesPointingHere = row
    ? aliases.filter((alias) =>
        Object.values(alias.resolves_to).includes(row.id),
      )
    : [];
  return (
    <Sheet open={!!row} onOpenChange={onOpenChange}>
      <SheetContent className="w-[92vw] overflow-y-auto sm:max-w-xl">
        {row && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>{row.name ?? row.display_name}</SheetTitle>
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
                <Fact
                  label="Provider"
                  value={row.provider ?? "Provider unknown"}
                />
                <Fact label="Location" value={localityLabel(row)} />
                <Fact label="Cost" value={costLabel(row)} />
                <Fact label="Typical time" value={timingLabel(row)} />
              </div>
              <div>
                <h3 className="font-medium">Availability</h3>
                <div className="mt-3 rounded-lg border p-3">
                  <Availability row={row} />
                </div>
              </div>
              <div>
                <h3 className="font-medium">Aliases that point here</h3>
                {row.alias ? (
                  <p className="text-muted-foreground mt-2 text-sm">
                    This row is a stable alias.
                  </p>
                ) : aliasesPointingHere.length ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {aliasesPointingHere.map((alias) => (
                      <Badge key={alias.id} variant="secondary">
                        {alias.id}
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="text-muted-foreground mt-2 text-sm">
                    No stable aliases currently point to this model.
                  </p>
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
                        key={receipt.request_id ?? index}
                        className="rounded-lg border p-3"
                      >
                        {receipt.status ?? "Recorded request"} ·{" "}
                        {typeof receipt.latency_ms === "number"
                          ? `${receipt.latency_ms} ms`
                          : "Duration not measured"}
                        {receipt.request_id && (
                          <a
                            className="mt-2 block min-h-11 content-center underline underline-offset-4"
                            href={`/r/${encodeURIComponent(receipt.request_id)}`}
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

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      variant={active ? "default" : "outline"}
      size="sm"
      onClick={onClick}
      aria-pressed={active}
    >
      {children}
    </Button>
  );
}

export default function ModelsClient() {
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState<Capability | "all">("all");
  const [showFree, setShowFree] = useState(true);
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
    queryFn: fetchCatalog,
  });
  const allRows = useMemo(
    () => [...(catalogData?.aliases ?? []), ...(catalogData?.models ?? [])],
    [catalogData],
  );
  const availableCapabilities = capabilityOrder.filter((cap) =>
    allRows.some((row) => hasCapability(row, cap)),
  );
  const passesFilters = (row: CatalogRow) => {
    if (!itemMatches(row, query)) return false;
    if (capability !== "all" && !hasCapability(row, capability)) {
      return false;
    }
    if (row.cost.kind === "free" && !showFree) return false;
    if (row.cost.kind === "paid" && !showPaid) return false;
    return true;
  };
  const aliasRows = (catalogData?.aliases ?? []).filter(passesFilters);
  const modelRows = (catalogData?.models ?? []).filter(passesFilters);
  const groupedAliases = capabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: aliasRows.filter((row) => primaryCapability(row) === cap),
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
              <div className="flex flex-wrap gap-2">
                <FilterButton
                  active={showFree}
                  onClick={() => setShowFree((value) => !value)}
                >
                  Free
                </FilterButton>
                <FilterButton
                  active={showPaid}
                  onClick={() => setShowPaid((value) => !value)}
                >
                  Paid
                </FilterButton>
              </div>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              <FilterButton
                active={capability === "all"}
                onClick={() => setCapability("all")}
              >
                All
              </FilterButton>
              {availableCapabilities.map((cap) => (
                <FilterButton
                  key={cap}
                  active={capability === cap}
                  onClick={() => setCapability(cap)}
                >
                  {capabilityLabels[cap]}
                </FilterButton>
              ))}
            </div>
          </section>
          {groupedAliases.length === 0 ? (
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
            />
          ) : (
            groupedAliases.map((group) => (
              <section key={group.capability} className="space-y-4">
                <div className="flex items-center gap-3">
                  <CapabilityIcon capability={group.capability} />
                  <div>
                    <h2 className="text-lg font-semibold">
                      {capabilityLabels[group.capability]}
                    </h2>
                    <p className="text-muted-foreground text-sm">
                      Stable aliases stay the same when the backing model
                      changes.
                    </p>
                  </div>
                </div>
                <div className="grid gap-4 lg:grid-cols-2">
                  {group.rows.map((row) => (
                    <AliasCard key={row.id} row={row} onDetail={setDetail} />
                  ))}
                </div>
              </section>
            ))
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
                      <TableHead>Provider</TableHead>
                      <TableHead>Cost</TableHead>
                      <TableHead>Status</TableHead>
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
                            onClick={() => setDetail(row)}
                          >
                            {row.name ?? row.display_name}
                          </Button>
                        </TableCell>
                        <TableCell>
                          {row.capabilities[0]?.label ??
                            capabilityLabels[primaryCapability(row)]}
                        </TableCell>
                        <TableCell>
                          {row.provider ?? "Provider unknown"}
                        </TableCell>
                        <TableCell>{costLabel(row)}</TableCell>
                        <TableCell>
                          {availabilityLabel(availabilityState(row))}
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
        aliases={catalogData?.aliases ?? []}
        onOpenChange={(open) => !open && setDetail(null)}
      />
    </div>
  );
}
