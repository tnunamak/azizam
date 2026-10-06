"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AudioLines,
  ArrowUpRight,
  GitBranch,
  Image as ImageInputIcon,
  Search,
  SlidersHorizontal,
  Type,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { capabilityLabels } from "@/lib/terms";
import { availabilityReason } from "@/lib/status-copy";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { CopyButton } from "@/components/ui/copy-button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
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
  StatusPill,
} from "@/components/product";
import { FilterToggle } from "@/components/product/filter-toggle";

import {
  readJson,
  type RecentAsset,
  type Workflow as WorkflowRow,
  type WorkflowInput,
  type WorkflowCatalog as WorkflowsCatalog,
} from "./workflow-api";
import { RelativeTime } from "@/components/product";
type WorkflowCapability = Capability | "audio" | "music" | "sfx" | "workflow";
type InputKind = "image" | "text" | "audio";
const EMPTY_WORKFLOWS: WorkflowRow[] = [];

const capabilityOrder: WorkflowCapability[] = [
  "image",
  "video",
  "music",
  "sfx",
  "audio",
  "workflow",
];

async function fetchWorkflows(): Promise<WorkflowsCatalog> {
  const response = await fetch("/me/api/workflows", {
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error("Workflows catalog could not be loaded. Try again.");
  }
  const payload = (await response.json()) as unknown;
  if (!isCatalog(payload)) {
    throw new Error("Workflows catalog response is not in the expected shape.");
  }
  return payload;
}

function isCatalog(value: unknown): value is WorkflowsCatalog {
  if (!value || typeof value !== "object") return false;
  const maybe = value as Partial<WorkflowsCatalog>;
  return maybe.schema_version === 1 && Array.isArray(maybe.workflows);
}

function capabilityLabel(value: string) {
  return (
    {
      image: capabilityLabels.image,
      video: capabilityLabels.video,
      voice: capabilityLabels.speech,
      music: capabilityLabels.music,
      sfx: capabilityLabels.sound_effects,
      audio: capabilityLabels.audio,
      workflow: "Workflows",
      chat: capabilityLabels.chat,
      transcribe: capabilityLabels.transcribe,
      decide: capabilityLabels.decisions,
      embed: capabilityLabels.embeddings,
    }[value as WorkflowCapability] ?? value
  );
}

function iconCapability(value: string): Capability {
  return ["audio", "music", "sfx"].includes(value)
    ? "voice"
    : [
          "image",
          "video",
          "voice",
          "chat",
          "transcribe",
          "decide",
          "embed",
        ].includes(value)
      ? (value as Capability)
      : "image";
}

function timeLabel(value: number | null): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return undefined;
  }
  if (value < 1) return `~${Math.round(value * 1000)} ms`;
  return `~${value.toFixed(value >= 10 ? 0 : 1)} s`;
}

function rangeLabel(input: WorkflowInput) {
  if (input.enum?.length) return input.enum.map(String).join(", ");
  if (input.minimum != null && input.maximum != null) {
    return `${input.minimum} to ${input.maximum}`;
  }
  if (input.minimum != null) return `At least ${input.minimum}`;
  if (input.maximum != null) return `At most ${input.maximum}`;
  return "";
}

function valueLabel(value: unknown) {
  if (value === undefined || value === null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function conciseWords(value: string, maxWords = 12) {
  const words = value.trim().split(/\s+/).filter(Boolean);
  if (words.length <= maxWords) return value.trim();
  return `${words.slice(0, maxWords).join(" ")}...`;
}

function purposeLabel(workflow: WorkflowRow) {
  const description = workflow.description.trim();
  if (!description) return "";
  const sentence = description.split(/(?<=[.!?])\s+/)[0];
  const jargon =
    /\b(contract|protocol|gateway-composed|engine|binding|bound|backend|schema|adapter|passthrough|checkpoint)\b|[\w.-]+\.(safetensors|ckpt|gguf)\b/i;
  if (jargon.test(sentence)) return "";
  return conciseWords(sentence);
}

function workflowInputKind(input: WorkflowInput): InputKind | null {
  const name = input.name.toLowerCase();
  const type = input.type.toLowerCase();
  const media = [input.media_type, ...(input.accepted_media_types ?? [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const imageFieldNames = new Set([
    "image",
    "input_image",
    "init_image",
    "mask_image",
    "source_image",
    "reference_image",
  ]);
  if (media.includes("image/")) {
    return "image";
  }
  if (media.includes("audio/")) return "audio";
  if (
    imageFieldNames.has(name) &&
    ["image", "file", "binary", "blob", "string"].includes(type)
  ) {
    return "image";
  }
  if (
    ["file", "upload", "blob", "binary"].includes(type) ||
    name.endsWith("_file")
  ) {
    return null;
  }
  if (type === "string") return "text";
  return null;
}

function matches(row: WorkflowRow, query: string) {
  if (!query) return true;
  const haystack = [
    row.id,
    row.title,
    row.description,
    ...row.aliases,
    row.engine_binding ?? "",
    row.native_route ?? "",
    ...row.inputs.flatMap((input) => [
      input.name,
      input.description ?? "",
      rangeLabel(input),
    ]),
    ...(row.outputs ?? []).flatMap((output) => [
      output.name,
      output.media_type ?? "",
      output.description ?? "",
    ]),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

// Types describe the manifest; they do not imply an engine is ready to run.
function requiredInputs(workflow: WorkflowRow) {
  return workflow.inputs.filter((input) => input.accepted && input.required);
}

function requiredInputLabel(workflow: WorkflowRow) {
  const required = requiredInputs(workflow);
  if (!workflow.inputs.length) return "Inputs not listed";
  if (!required.length) return "No required inputs";
  return required.map((input) => input.name).join(", ");
}

function outputKinds(workflow: WorkflowRow) {
  const kinds = [
    ...new Set(
      (workflow.outputs ?? []).map(
        (output) => output.media_type || output.name,
      ),
    ),
  ];
  return (
    kinds.join(", ") ||
    (workflow.capability === "workflow"
      ? ""
      : capabilityLabel(workflow.capability))
  );
}

function WorkflowInputIcons({ workflow }: { workflow: WorkflowRow }) {
  const kinds = [
    ...new Set(
      workflow.inputs
        .filter((input) => input.accepted)
        .map(workflowInputKind)
        .filter((kind): kind is InputKind => kind != null),
    ),
  ];
  const icons: Record<InputKind, [typeof Type, string]> = {
    image: [ImageInputIcon, "Image"],
    text: [Type, "Text"],
    audio: [AudioLines, "Audio"],
  };
  const needsImage = requiredInputs(workflow).some(
    (input) => workflowInputKind(input) === "image",
  );
  if (!kinds.length && !needsImage) return null;
  return (
    <div
      role="group"
      aria-label="Inputs"
      className="flex min-h-8 flex-wrap items-center gap-2"
    >
      {kinds.map((kind) => {
        const [Icon, label] = icons[kind];
        return (
          <span
            key={kind}
            role="img"
            aria-label={`${label} input`}
            title={`${label} input`}
            className="text-muted-foreground inline-flex size-8 items-center justify-center rounded-md border"
          >
            <Icon aria-hidden="true" className="size-4" />
          </span>
        );
      })}
      {needsImage && <Badge variant="outline">Needs an image</Badge>}
    </div>
  );
}

function WorkflowCard({
  workflow,
  onDetail,
}: {
  workflow: WorkflowRow;
  onDetail: (workflow: WorkflowRow) => void;
}) {
  return (
    <article
      aria-label={workflow.title}
      className="bg-card text-card-foreground flex min-w-0 flex-col gap-5 rounded-xl border p-5"
    >
      <div className="flex min-w-0 items-start gap-3">
        <CapabilityIcon capability={iconCapability(workflow.capability)} />
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold break-words">{workflow.title}</h3>
          <p className="text-muted-foreground mt-2 line-clamp-2 text-sm">
            {purposeLabel(workflow)}
          </p>
          <div className="mt-3">
            <WorkflowInputIcons workflow={workflow} />
          </div>
        </div>
      </div>
      {!(["ready", "idle"].includes(workflow.availability.status)) && (
        <StatusPill
          status={
            workflow.availability.status === "not_configured"
              ? "unavailable"
              : workflow.availability.status
          }
          summary
        />
      )}
      <div className="mt-auto flex flex-wrap gap-2">
        <Button asChild size="sm">
          <Link href={`/try/?workflow=${encodeURIComponent(workflow.id)}`}>
            Run in Try
          </Link>
        </Button>
        <Button variant="outline" size="sm" onClick={() => onDetail(workflow)}>
          Details
        </Button>
      </div>
    </article>
  );
}

function LoadingWorkflows() {
  return (
    <div aria-label="Loading workflows" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-11 w-56" />
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

function DetailSheet({
  workflow,
  onOpenChange,
}: {
  workflow: WorkflowRow | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={!!workflow} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:!max-w-5xl [&_a[data-slot=button]]:min-h-11 [&_button]:min-h-11">
        {workflow && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>{workflow.title}</SheetTitle>
              <SheetDescription>
                Inputs, example requests and recent outputs.
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <section className="space-y-3">
                <h3 className="font-medium">Purpose</h3>
                {purposeLabel(workflow) && (
                  <p className="text-muted-foreground text-sm break-words">
                    {purposeLabel(workflow)}
                  </p>
                )}
                <div className="grid gap-3 text-sm sm:grid-cols-2">
                  {workflow.inputs.length > 0 && (
                    <Fact
                      label="Required inputs"
                      value={requiredInputLabel(workflow)}
                    />
                  )}
                  {outputKinds(workflow) && (
                    <Fact label="Output" value={outputKinds(workflow)} />
                  )}
                </div>
              </section>
              <details className="rounded-lg border p-4">
                <summary className="focus-visible:outline-ring min-h-11 cursor-pointer py-3 font-medium focus-visible:outline-2">
                  Technical details
                </summary>
                <div className="mt-3 space-y-4">
                  {workflow.technical_notes && (
                    <p className="text-muted-foreground text-sm break-words whitespace-pre-wrap">
                      {workflow.technical_notes}
                    </p>
                  )}
                  {availabilityReason(
                    workflow.availability.status,
                    workflow.availability.reason_code,
                  ) && (
                    <p className="text-muted-foreground text-sm break-words">
                      {availabilityReason(
                        workflow.availability.status,
                        workflow.availability.reason_code,
                      )}
                    </p>
                  )}
                  <div className="grid gap-4 text-sm sm:grid-cols-2">
                    <Fact
                      label="Workflow"
                      value={
                        <span className="inline-flex max-w-full items-center gap-2">
                          <span className="break-all">{workflow.id}</span>
                          <CopyButton
                            value={workflow.id}
                            aria-label="Copy workflow ID"
                          />
                        </span>
                      }
                    />
                    <Fact
                      label="Capability"
                      value={capabilityLabel(workflow.capability)}
                    />
                    <Fact
                      label="Engine binding"
                      value={
                        workflow.engine_binding ??
                        "No engine binding configured"
                      }
                    />
                    {timeLabel(workflow.typical_seconds) && (
                      <Fact
                        label="Typical time"
                        value={timeLabel(workflow.typical_seconds)}
                      />
                    )}
                    <Fact
                      label="Native route"
                      value={
                        workflow.native_route ??
                        "No native passthrough route configured"
                      }
                    />
                    <Fact
                      label="Discovery"
                      value={
                        <a
                          className="underline underline-offset-4"
                          href={workflow.discovery_url}
                        >
                          Machine-readable workflow
                        </a>
                      }
                    />
                  </div>
                  <div>
                    <h3 className="font-medium">Stable aliases</h3>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {workflow.aliases.length ? (
                        workflow.aliases.map((alias) => (
                          <Badge
                            key={alias}
                            variant="secondary"
                            className="max-w-full break-all whitespace-normal"
                          >
                            {alias}
                          </Badge>
                        ))
                      ) : (
                        <p className="text-muted-foreground text-sm">
                          No stable aliases point here.
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              </details>
              <div>
                <h3 className="font-medium">Inputs</h3>
                <p className="text-muted-foreground mt-1 text-sm">
                  Inputs marked Applies change the request. Ignored inputs have
                  no effect on this route.
                </p>
                <ul
                  aria-label="Input summaries"
                  className="mt-3 divide-y rounded-lg border px-4 sm:hidden"
                >
                  {workflow.inputs.map((input) => (
                    <li key={input.name} className="space-y-2 py-4">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <h4 className="font-mono text-sm break-all">
                            {input.name}
                          </h4>
                          <p className="text-muted-foreground text-xs">
                            {input.type}
                            {input.required ? " · Required" : " · Optional"}
                          </p>
                        </div>
                        <Badge
                          variant={input.accepted ? "secondary" : "outline"}
                        >
                          {input.accepted ? "Applies" : "Ignored"}
                        </Badge>
                      </div>
                      {input.description && (
                        <p className="text-muted-foreground text-sm break-words">
                          {input.description}
                        </p>
                      )}
                      <dl className="space-y-2 text-sm">
                        {input.default != null && (
                          <div>
                            <dt className="text-muted-foreground text-xs">
                              Default
                            </dt>
                            <dd className="break-words">
                              {valueLabel(input.default)}
                            </dd>
                          </div>
                        )}
                        {rangeLabel(input) && (
                          <div>
                            <dt className="text-muted-foreground text-xs">
                              Range
                            </dt>
                            <dd className="break-words">{rangeLabel(input)}</dd>
                          </div>
                        )}
                      </dl>
                    </li>
                  ))}
                </ul>
                <div
                  className="mt-3 hidden max-w-full overflow-x-auto rounded-lg border sm:block [&>div]:overflow-visible"
                  tabIndex={0}
                  aria-label="Scrollable inputs"
                >
                  <Table className="min-w-[40rem] table-fixed">
                    <TableHeader>
                      <TableRow>
                        <TableHead>Name</TableHead>
                        <TableHead>Type</TableHead>
                        <TableHead>Default</TableHead>
                        <TableHead>Range</TableHead>
                        <TableHead>Applies</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {workflow.inputs.map((input) => (
                        <TableRow key={input.name}>
                          <TableCell className="font-mono text-xs break-all !whitespace-normal">
                            {input.name}
                            {input.required && (
                              <span className="ml-1 text-xs">(required)</span>
                            )}
                            {input.description && (
                              <p className="text-muted-foreground mt-1 font-sans text-xs">
                                {input.description}
                              </p>
                            )}
                          </TableCell>
                          <TableCell>{input.type}</TableCell>
                          <TableCell className="max-w-48 break-words !whitespace-normal">
                            {valueLabel(input.default)}
                          </TableCell>
                          <TableCell className="max-w-48 break-words !whitespace-normal">
                            {rangeLabel(input)}
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={
                                !input.accepted ? "outline" : "secondary"
                              }
                            >
                              {input.accepted ? "Applies" : "Ignored"}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
              <div>
                <h3 className="font-medium">Example request</h3>
                <div className="mt-3 [&_pre]:overflow-visible [&_pre]:break-all [&_pre]:whitespace-pre-wrap">
                  <CodeBlock
                    snippets={[
                      { language: "curl", code: workflow.examples.curl },
                      { language: "Python", code: workflow.examples.python },
                      {
                        language: "JavaScript",
                        code: workflow.examples.javascript,
                      },
                    ]}
                  />
                </div>
              </div>
              <RecentJobs key={workflow.id} workflow={workflow} />
              <div className="flex flex-wrap gap-2">
                <Button asChild>
                  <Link
                    href={`/try/?workflow=${encodeURIComponent(workflow.id)}`}
                  >
                    Run in Try
                  </Link>
                </Button>
                <Button asChild variant="outline">
                  <a href={"/me/api/workflows"}>
                    Discovery list
                    <ArrowUpRight aria-hidden="true" />
                  </a>
                </Button>
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="mt-1 font-medium break-words">{value}</div>
    </div>
  );
}

function RecentJobs({ workflow }: { workflow: WorkflowRow }) {
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflow-media", workflow.id],
    queryFn: () =>
      readJson<{ assets: RecentAsset[] }>(
        `/me/api/media?${new URLSearchParams({ workflow: workflow.id, limit: "5" })}`,
      ),
    retry: false,
  });
  return (
    <section aria-label="Recent jobs">
      <h3 className="font-medium">Recent jobs</h3>
      <p className="text-muted-foreground mt-1 text-sm">
        Your latest outputs from this workflow, with their original request
        details.
      </p>
      {isPending ? (
        <Skeleton
          role="status"
          aria-label="Loading recent jobs"
          className="mt-4 h-24"
        />
      ) : isError ? (
        <div className="mt-4">
          <ErrorState
            message="Recent jobs could not be loaded. Try again."
            onRetry={() => void refetch()}
            retrying={isFetching}
          />
        </div>
      ) : !data?.assets.length ? (
        <p className="text-muted-foreground mt-4 text-sm">
          No recent jobs yet. Run this workflow in Try to create your first
          result.
        </p>
      ) : (
        <ul className="mt-4 divide-y">
          {data.assets.map((asset) => (
            <li key={asset.asset_id} className="space-y-2 py-4 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {asset.kind.charAt(0).toUpperCase() + asset.kind.slice(1)}{" "}
                  output
                </span>
                <RelativeTime
                  ts={asset.created_at}
                  fallback=""
                  className="text-muted-foreground text-xs"
                />
              </div>
              {asset.prompt && <p className="break-words">{asset.prompt}</p>}
              {(asset.model || asset.workflow || asset.generation_ms != null) && (
                <dl className="text-muted-foreground grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                  {asset.model && (
                    <>
                      <dt>Model</dt>
                      <dd className="break-all">{asset.model}</dd>
                    </>
                  )}
                  {asset.workflow && (
                    <>
                      <dt>Workflow</dt>
                      <dd className="break-all">{asset.workflow}</dd>
                    </>
                  )}
                  {asset.generation_ms != null && (
                    <>
                      <dt>Duration</dt>
                      <dd>{`${(asset.generation_ms / 1000).toFixed(1)} s`}</dd>
                    </>
                  )}
                </dl>
              )}
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" asChild>
                  <Link
                    href={`/library/?asset=${encodeURIComponent(asset.asset_id)}`}
                  >
                    Open in Library
                  </Link>
                </Button>
                {asset.request_id ? (
                  <Button variant="outline" size="sm" asChild>
                    <a href={`/r/${encodeURIComponent(asset.request_id)}`}>
                      View receipt
                    </a>
                  </Button>
                ) : null}
                {asset.download_url && (
                  <Button variant="outline" size="sm" asChild>
                    <a href={asset.download_url} download>
                      Download output
                    </a>
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function WorkflowsPage() {
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState<WorkflowCapability | "all">(
    "all",
  );
  const [detail, setDetail] = useState<WorkflowRow | null>(null);
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflows", "catalog"],
    queryFn: fetchWorkflows,
    retry: false,
  });
  const rows = data?.workflows ?? EMPTY_WORKFLOWS;
  const availableCapabilities = capabilityOrder.filter((cap) =>
    rows.some((row) => row.capability === cap),
  );
  const filtered = useMemo(
    () =>
      rows.filter(
        (row) =>
          matches(row, query) &&
          (capability === "all" || row.capability === capability),
      ),
    [rows, query, capability],
  );
  const grouped = capabilityOrder
    .map((cap) => ({
      capability: cap,
      rows: filtered.filter((row) => row.capability === cap),
    }))
    .filter((group) => group.rows.length > 0);

  return (
    <div className="space-y-8 [&_a[data-slot=button]]:min-h-11 [&_button]:min-h-11">
      <PageHeader
        title="Workflows"
        subtitle="Choose by purpose and inputs."
        actions={
          isFetching ? (
            <Badge variant="outline">Refreshing</Badge>
          ) : data ? (
            <Badge variant="outline">
              {data.counts?.workflows ?? rows.length} workflows
            </Badge>
          ) : undefined
        }
      />
      {isPending ? (
        <LoadingWorkflows />
      ) : isError ? (
        <ErrorState
          message="Workflows catalog could not be loaded. Try again."
          onRetry={() => void refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={GitBranch}
          title="No workflows available"
          description="Workflows appear here when configured and allowed by your access plan. Ask the gateway owner to connect a workflow engine and grant access."
          action={
            <Button variant="outline" asChild>
              <Link href="/models">Explore models</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <label className="relative min-w-0 flex-1 lg:max-w-md">
              <Search
                aria-hidden="true"
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search workflows"
                className="pl-9"
              />
              <span className="sr-only">Search workflows</span>
            </label>
            <div className="flex flex-wrap gap-2">
              <FilterToggle
                pressed={capability === "all"}
                size="sm"
                onClick={() => setCapability("all")}
              >
                <SlidersHorizontal aria-hidden="true" />
                All
              </FilterToggle>
              {availableCapabilities.map((cap) => (
                <FilterToggle
                  key={cap}
                  pressed={capability === cap}
                  size="sm"
                  onClick={() => setCapability(cap)}
                >
                  {capabilityLabel(cap)}
                </FilterToggle>
              ))}
            </div>
          </div>
          {filtered.length === 0 ? (
            <EmptyState
              icon={Search}
              title="No matching workflows"
              description="Clear search or choose another capability."
              action={
                <Button
                  variant="outline"
                  onClick={() => {
                    setQuery("");
                    setCapability("all");
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <div
              aria-label="Available workflows"
              className={cn(
                "grid gap-6",
                filtered.length > 1 && "sm:grid-cols-2",
                filtered.length > 2 && "lg:grid-cols-3",
              )}
            >
              {grouped.map((group) => (
                <section
                  key={group.capability}
                  aria-label={`${capabilityLabel(group.capability)} workflows`}
                  className={cn(
                    "min-w-0 space-y-3",
                    group.rows.length > 1 && "sm:col-span-2",
                    group.rows.length > 2 && "lg:col-span-3",
                  )}
                >
                  <h2 className="text-lg font-semibold">
                    {capabilityLabel(group.capability)}
                  </h2>
                  <div
                    className={cn(
                      "grid gap-4",
                      group.rows.length > 1 && "sm:grid-cols-2",
                      group.rows.length > 2 && "lg:grid-cols-3",
                    )}
                  >
                    {group.rows.map((workflow) => (
                      <WorkflowCard
                        key={workflow.id}
                        workflow={workflow}
                        onDetail={setDetail}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </>
      )}
      <DetailSheet
        workflow={detail}
        onOpenChange={(open) => !open && setDetail(null)}
      />
    </div>
  );
}
