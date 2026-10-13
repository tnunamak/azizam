"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  GitBranch,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { capabilityLabels } from "@/lib/terms";
import {
  MEDIA_NOUN,
  declaredMediaTypes,
  isMediaInput,
  mediaFamily,
  mediaKindOf,
  withArticle,
  type MediaKind,
} from "@/lib/workflow-media-input";
import { useModelName } from "@/lib/use-model-name";
import { useViewer } from "@/components/shell/session";
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
import type { Capability } from "@/components/product/capability-icon";
import {
  CodeBlock,
  EmptyState,
  ErrorState,
  PageHeader,
  StatusPill,
} from "@/components/product";
import { StateBadge } from "@/components/product/status-ui";
import { ListCard, ListRow } from "@/components/product/list-card";
import { Segmented } from "@/components/product/segmented";
import {
  workflowAvailabilityView,
  type WorkflowReadiness,
} from "@/lib/workflow-availability";

import {
  readRecentAssets,
  isWorkflowCatalog,
  type Workflow as WorkflowRow,
  type WorkflowInput,
  type WorkflowOutput,
  type WorkflowCatalog as WorkflowsCatalog,
} from "./workflow-api";
import { RelativeTime } from "@/components/product";
type WorkflowCapability = Capability | "audio" | "music" | "sfx" | "workflow";
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
  if (!isWorkflowCatalog(payload)) {
    throw new Error("Workflows catalog response is not in the expected shape.");
  }
  return payload;
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
    }[value as WorkflowCapability] ??
    value.replace(/[_-]+/g, " ").replace(/^./, (letter) => letter.toUpperCase())
  );
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


function plainValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}
function plainRange(input: WorkflowInput): string {
  return input.enum?.length ? input.enum.map(plainValue).filter(Boolean).join(", ") : rangeLabel(input);
}
function acceptedFiles(input: WorkflowInput): string {
  const types = declaredMediaTypes(input);
  if (!types.length) return "File formats not listed";
  const formats: Record<string, string> = {
    "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP",
    "audio/wav": "WAV", "audio/mpeg": "MP3", "video/mp4": "MP4",
    "application/pdf": "PDF", "text/plain": "plain text",
  };
  return joinWords([...new Set(types.map((type) => formats[type] ?? mediaLabel(type, "a file")))], "or");
}

function purposeLabel(workflow: WorkflowRow) {
  if (workflow.purpose?.trim()) return workflow.purpose.trim();
  const description = workflow.description.trim();
  if (!description) return "";
  const sentence = description.split(/(?<=[.!?])\s+/)[0];
  const jargon =
    /\b(contract|protocol|gateway-composed|engine|binding|bound|backend|schema|adapter|passthrough|checkpoint)\b|[\w.-]+\.(safetensors|ckpt|gguf)\b/i;
  if (jargon.test(sentence)) return "";
  return sentence;
}

function matches(row: WorkflowRow, query: string) {
  if (!query) return true;
  const haystack = [
    row.id,
    row.title,
    row.description,
    purposeLabel(row),
    inputSummary(row, true),
    inputSummary(row, false),
    outputSummary(row),
    ...row.aliases,
    row.engine_binding ?? "",
    row.native_route ?? "",
    ...row.inputs.flatMap((input) => [
      input.name,
      input.label ?? "",
      input.description ?? "",
      rangeLabel(input),
    ]),
    ...(row.outputs ?? []).flatMap((output) => [
      output.name,
      output.label ?? "",
      output.media_type ?? "",
      output.description ?? "",
    ]),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

// Describe only declared metadata, never infer a need from a field name or icon.
function joinWords(values: string[], conjunction = "and") {
  if (values.length < 2) return values[0] ?? "";
  return `${values.slice(0, -1).join(", ")} ${conjunction} ${values.at(-1)}`;
}

function mediaLabel(value: string, fallback: string) {
  const family = mediaFamily(value);
  if (family === "text") return "text";
  return family in MEDIA_NOUN
    ? withArticle(MEDIA_NOUN[family as MediaKind])
    : fallback;
}

function plainName(name: string) {
  return name.replace(/[_-]+/g, " ").trim().toLowerCase();
}

/** The best noun for one input: the manifest's label, its media kind, then its type. */
function inputNoun(input: WorkflowInput) {
  const label = input.label?.trim();
  if (label) return label;
  const media = declaredMediaTypes(input);
  if (media.length) {
    return joinWords([...new Set(media.map((value) => mediaLabel(value, "a file")))], "or");
  }
  if (isMediaInput(input)) {
    const kind = mediaKindOf(input);
    return kind ? withArticle(MEDIA_NOUN[kind]) : "a file";
  }
  switch (input.type.toLowerCase()) {
    case "string": return "text";
    case "number":
    case "integer": return "a number";
    case "boolean": return "a yes-or-no choice";
    case "array": return "a list";
    case "object": return "structured information";
    default: return "an input";
  }
}

function outputNoun(output: WorkflowOutput) {
  const label = output.label?.trim();
  if (label) return label;
  const family = output.media_type ? mediaFamily(output.media_type) : "";
  if (["image", "audio", "video", "text"].includes(family)) {
    return mediaLabel(output.media_type!, "");
  }
  return output.name.trim() ? withArticle(plainName(output.name)) : "an output";
}

/** Nouns for the accepted inputs. Two inputs never share a noun: each is named by its field. */
function inputNouns(inputs: WorkflowInput[]) {
  const nouns = inputs.map(inputNoun);
  return new Map(
    inputs.map((input, index) => [
      input,
      nouns.filter((noun) => noun === nouns[index]).length > 1
        ? `${nouns[index]} for ${plainName(input.name)}`
        : nouns[index],
    ]),
  );
}

function inputSummary(workflow: WorkflowRow, required: boolean) {
  const accepted = workflow.inputs.filter((input) => input.accepted);
  const nouns = inputNouns(accepted);
  return joinWords(
    accepted.filter((input) => !!input.required === required).map((input) => nouns.get(input)!),
  );
}

function outputSummary(workflow: WorkflowRow) {
  return joinWords([...new Set((workflow.outputs ?? []).map(outputNoun))]) || "output not listed";
}

const outputGroups = ["Pictures", "Video", "Audio", "Text", "Mixed outputs", "Other outputs", "Output not listed"];

function outputGroup(workflow: WorkflowRow) {
  const outputs = workflow.outputs ?? [];
  if (!outputs.length) return "Output not listed";
  const groups = new Set(outputs.map((output) => {
    const family = output.media_type ? mediaFamily(output.media_type) : "";
    switch (family) {
      case "image": return "Pictures";
      case "video": return "Video";
      case "audio": return "Audio";
      case "text": return "Text";
      default: return "Other outputs";
    }
  }));
  return groups.size === 1 ? [...groups][0] : "Mixed outputs";
}

function WorkflowSummary({ workflow }: { workflow: WorkflowRow }) {
  const required = inputSummary(workflow, true);
  const optional = inputSummary(workflow, false);
  return (
    <div aria-label="What you need and get" className="space-y-1 text-meta">
      <p>Needs: {required || (workflow.inputs.length ? "no required inputs" : "inputs not listed")}</p>
      {optional && <p>Optional: {optional}</p>}
      <p>Makes: {outputSummary(workflow)}</p>
    </div>
  );
}

// Who can act on a workflow comes from the shared mapping. "setup" needs the
// owner, so members do not see it; everything else, including an engine the
// gateway names as down, is shown to everyone.
function readiness(workflow: WorkflowRow): WorkflowReadiness {
  return workflowAvailabilityView(workflow.availability, { owner: false })
    .readiness;
}

function WorkflowAction({
  workflow,
  owner,
  hideReadyStatus = false,
}: {
  workflow: WorkflowRow;
  owner: boolean;
  hideReadyStatus?: boolean;
}) {
  const view = workflowAvailabilityView(workflow.availability, {
    owner,
    engineBinding: workflow.engine_binding,
  });
  const status = workflow.availability.status;
  if (view.runnable) {
    const showStatus = !hideReadyStatus || status !== "ready";
    return (
      <div className="flex flex-wrap items-center gap-2">
        {showStatus && (
          <StatusPill status={status} reason={view.text ?? undefined} summary />
        )}
        <Button asChild size="sm" variant={hideReadyStatus ? "ghost" : "default"}>
          <Link prefetch={false} href={`/try/?workflow=${encodeURIComponent(workflow.id)}`}>
            Open in Try
          </Link>
        </Button>
      </div>
    );
  }
  const ownerSetup = owner && view.readiness === "setup";
  return (
    <div className="flex max-w-sm flex-wrap items-center gap-2 max-sm:max-w-40">
      {(view.readiness === "wait" || view.readiness === "down") && (
        <StatusPill status={status} summary />
      )}
      <div className="flex flex-wrap items-center gap-2">
        {!ownerSetup && (
          <Button size="sm" variant={hideReadyStatus ? "ghost" : "default"} disabled>
            Open in Try
          </Button>
        )}
        <p className="text-muted-foreground text-meta">{view.text}</p>
        {view.fix && (
          <Button asChild variant="outline" size="sm">
            <Link prefetch={false} href={view.fix.href}>{view.fix.action}</Link>
          </Button>
        )}
      </div>
    </div>
  );
}

function WorkflowCard({
  workflow,
  owner,
  onDetail,
}: {
  workflow: WorkflowRow;
  owner: boolean;
  onDetail: (workflow: WorkflowRow) => void;
}) {
  const needs = inputSummary(workflow, true) ||
    (workflow.inputs.length ? "no required inputs" : "inputs not listed");
  const summary = `From ${needs} → ${outputSummary(workflow)}`;
  return (
    <ListRow
      lines={2}
      aria-label={workflow.title}
      className="hover:bg-muted/50 cursor-pointer h-auto! min-h-(--row-2) [&_p]:whitespace-normal [&_p]:overflow-visible [&_p]:text-clip"
      onClick={(event) => {
        if (!(event.target as HTMLElement).closest("a, button")) onDetail(workflow);
      }}
      title={
        <button
          type="button"
          aria-label={`Details for ${workflow.title}`}
          className="rounded-control text-left after:absolute after:inset-0 after:content-[''] hover:underline"
          onClick={() => onDetail(workflow)}
        >
          {workflow.title}
        </button>
      }
      meta={<span className="block break-words">{purposeLabel(workflow) && <span className="mb-1 block">{purposeLabel(workflow)}</span>}<span className="block">{summary}</span></span>}
      metaTitle={summary}
      trailing={<WorkflowAction workflow={workflow} owner={owner} hideReadyStatus />}
    />
  );
}

function LoadingWorkflows() {
  return (
    <div aria-label="Loading workflows" aria-busy="true" role="status">
      <div className="mb-6 flex flex-wrap gap-3">
        <Skeleton className="h-8 max-sm:h-11 w-56" />
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

function DetailSheet({
  workflow,
  owner,
  onOpenChange,
}: {
  workflow: WorkflowRow | null;
  owner: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const inputLabels = inputNouns(workflow?.inputs ?? []);
  const license = workflow && "license" in workflow && typeof workflow.license === "string"
    ? workflow.license
    : null;
  return (
    <Sheet open={!!workflow} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:!max-w-5xl">
        {workflow && (
          <>
            <SheetHeader className="border-b pr-12">
              <SheetTitle>{workflow.title}</SheetTitle>
              <SheetDescription>
                What you need, what you get and how to start.
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-6 p-4">
              <section className="space-y-3">
                {purposeLabel(workflow) && <h3 className="font-medium">Purpose</h3>}
                {purposeLabel(workflow) && (
                  <p className="text-muted-foreground text-meta break-words">
                    {purposeLabel(workflow)}
                  </p>
                )}
                <WorkflowSummary workflow={workflow} />
                {owner && license && (
                  <div className="space-y-1">
                    {/not for use|non-commercial/i.test(license) && (
                      <StateBadge state="degraded" word="Licence limit" />
                    )}
                    <p className="text-meta break-words">Licence: {license}</p>
                  </div>
                )}
              </section>
              <WorkflowAction workflow={workflow} owner={owner} />
              <details className="rounded-panel border p-4">
                <summary className="focus-visible:outline-ring min-h-8 max-sm:min-h-11 cursor-pointer py-1.5 font-medium focus-visible:outline-2">
                  {owner ? "Technical details" : "Input details"}
                </summary>
                <div className="mt-3 space-y-4">
                  {/* Research and engine notes are for the owner, never a member. */}
                  {owner && workflow.technical_notes && (
                    <p className="text-muted-foreground text-meta break-words whitespace-pre-wrap">
                      {workflow.technical_notes}
                    </p>
                  )}
                  <div className="grid gap-4 text-meta sm:grid-cols-2">
                    {owner && <Fact
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
                    />}
                    <Fact
                      label="Task"
                      value={capabilityLabel(workflow.capability)}
                    />
                    <Fact
                      label={owner ? "Input media types" : "Accepted files"}
                      value={workflow.inputs.filter((input) => owner || isMediaInput(input)).map((input) =>
                        owner ? `${input.name}: ${declaredMediaTypes(input).join(", ") || "not listed"}` : `${inputLabels.get(input)}: ${acceptedFiles(input)}`,
                      ).join("; ") || "Not listed"}
                    />
                    <Fact
                      label={owner ? "Output names and types" : "Outputs"}
                      value={(workflow.outputs ?? []).map((output) =>
                        owner ? `${output.name}: ${output.media_type || "not listed"}` : outputNoun(output),
                      ).join("; ") || "Not listed"}
                    />
                    {owner && (
                      <Fact
                        label="Engine"
                        value={workflow.engine_binding ?? "No engine configured"}
                      />
                    )}
                    {timeLabel(workflow.typical_seconds) && (
                      <Fact
                        label="Typical time"
                        value={timeLabel(workflow.typical_seconds)}
                      />
                    )}
                    {owner && (
                      <Fact
                        label="Native route"
                        value={
                          workflow.native_route ??
                          "No native passthrough route configured"
                        }
                      />
                    )}
                    {owner && <Fact
                      label="Discovery"
                      value={
                        <a
                          className="underline underline-offset-4"
                          href={workflow.discovery_url}
                        >
                          Machine-readable workflow
                        </a>
                      }
                    />}
                  </div>
                  <div>
                    <h3 className="font-medium">Model names</h3>
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
                        <p className="text-muted-foreground text-meta">
                          No other model names
                        </p>
                      )}
                    </div>
                  </div>
                  <div>
                    <h3 className="font-medium">Inputs</h3>
                    <p className="text-muted-foreground mt-1 text-meta">
                      Inputs marked Applies change the request. Ignored inputs have
                      no effect on this route.
                    </p>
                    <ul
                      aria-label="Input summaries"
                      className="mt-3 divide-y rounded-panel border px-4 sm:hidden"
                    >
                      {workflow.inputs.map((input) => (
                        <li key={input.name} className="space-y-2 py-4">
                          <div className="flex flex-wrap items-start justify-between gap-2">
                            <div className="min-w-0">
                              <h4
                                className={cn(
                                  "text-meta break-all",
                                  owner && "font-mono",
                                )}
                              >
                                {owner ? input.name : inputLabels.get(input)}
                              </h4>
                              <p className="text-muted-foreground text-label">
                                {owner ? input.type : inputNoun(input)}
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
                            <p className="text-muted-foreground text-meta break-words">
                              {input.description}
                            </p>
                          )}
                          <dl className="space-y-2 text-meta">
                            {(owner ? input.default != null : plainValue(input.default) !== "") && (
                              <div>
                                <dt className="text-muted-foreground text-label">
                                  Default
                                </dt>
                                <dd className="break-words">
                                  {owner ? valueLabel(input.default) : plainValue(input.default)}
                                </dd>
                              </div>
                            )}
                            {(owner ? rangeLabel(input) : plainRange(input)) && (
                              <div>
                                <dt className="text-muted-foreground text-label">
                                  Range
                                </dt>
                                <dd className="break-words">{owner ? rangeLabel(input) : plainRange(input)}</dd>
                              </div>
                            )}
                          </dl>
                        </li>
                      ))}
                    </ul>
                    <div
                      className="mt-3 hidden max-w-full overflow-x-auto rounded-panel border sm:block [&>div]:overflow-visible"
                      tabIndex={0}
                      aria-label="Scrollable inputs"
                    >
                      <Table className="min-w-160 table-fixed">
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
                              <TableCell className="font-mono text-label break-all !whitespace-normal">
                                {owner ? input.name : inputLabels.get(input)}
                                {input.required && (
                                  <span className="ml-1 text-label">(required)</span>
                                )}
                                {input.description && (
                                  <p className="text-muted-foreground mt-1 font-sans text-label">
                                    {input.description}
                                  </p>
                                )}
                              </TableCell>
                              <TableCell>{owner ? input.type : inputNoun(input)}</TableCell>
                              <TableCell className="max-w-48 break-words !whitespace-normal">
                                {owner ? valueLabel(input.default) : plainValue(input.default)}
                              </TableCell>
                              <TableCell className="max-w-48 break-words !whitespace-normal">
                                {owner ? rangeLabel(input) : plainRange(input)}
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
                  {owner && <div>
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
                  </div>}
                  {owner && <div className="flex flex-wrap gap-2">
                    <Button asChild variant="outline">
                      <a href={"/me/api/workflows"}>
                        Discovery list
                        <ArrowUpRight aria-hidden="true" />
                      </a>
                    </Button>
                  </div>}
                </div>
              </details>
              <RecentJobs key={workflow.id} workflow={workflow} owner={owner} />
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
      <div className="text-muted-foreground text-label">{label}</div>
      <div className="mt-1 font-medium break-words">{value}</div>
    </div>
  );
}

function RecentJobs({ workflow, owner }: { workflow: WorkflowRow; owner: boolean }) {
  const nameModel = useModelName();
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflow-media", workflow.id],
    queryFn: () =>
      readRecentAssets(
        `/me/api/media?${new URLSearchParams({ workflow: workflow.id, limit: "5" })}`,
      ),
    retry: false,
  });
  return (
    <section aria-label="Recent creations">
      <h3 className="font-medium">Recent creations</h3>
      <p className="text-muted-foreground mt-1 text-meta">
        Your latest outputs from this workflow, with their original request
        details.
      </p>
      {isPending ? (
        <Skeleton
          role="status"
          aria-label="Loading recent creations"
          className="mt-4 h-24"
        />
      ) : isError ? (
        <div className="mt-4">
          <ErrorState
            message="Recent creations could not be loaded. Try again."
            onRetry={() => void refetch()}
            retrying={isFetching}
          />
        </div>
      ) : !data?.assets.length ? (
        <p className="text-muted-foreground mt-4 text-meta">
          {/* The sheet already states why a workflow cannot run. */}
          {workflowAvailabilityView(workflow.availability, { owner, engineBinding: workflow.engine_binding }).runnable
            ? "No creations yet. Open this workflow in Try."
            : "No creations yet."}
        </p>
      ) : (
        <ul className="mt-4 divide-y">
          {data.assets.map((asset) => (
            <li key={asset.asset_id} className="space-y-2 py-4 text-meta">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {asset.kind.charAt(0).toUpperCase() + asset.kind.slice(1)}{" "}
                  output
                </span>
                <RelativeTime
                  ts={asset.created_at}
                  fallback=""
                  className="text-muted-foreground text-label"
                />
              </div>
              {asset.prompt && <p className="break-words">{asset.prompt}</p>}
              {(asset.model || asset.workflow || asset.generation_ms != null) && (
                <dl className="text-muted-foreground grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-label">
                  {asset.model && (
                    <>
                      <dt>Model</dt>
                      <dd className="break-all">{nameModel(asset.model)}</dd>
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
              {owner && asset.model && (
                <details>
                  <summary className="cursor-pointer text-xs">Technical details</summary>
                  <p className="mt-2 break-all font-mono text-xs">{asset.model}</p>
                </details>
              )}
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" asChild>
                  <Link prefetch={false}
                    href={`/library/?asset=${encodeURIComponent(asset.asset_id)}`}
                  >
                    Open in Library
                  </Link>
                </Button>
                {asset.request_id ? (
                  <Button variant="outline" size="sm" asChild>
                    <Link prefetch={false} href={`/usage/?receipt=${encodeURIComponent(asset.request_id)}`}>
                      View receipt
                    </Link>
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
  // Owner-only details use the same rule as Usage and Activity; a delegated
  // admin is not the owner.
  const owner = ["owner", "superadmin"].includes(useViewer().role);
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState<string>("all");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["workflows", "catalog"],
    queryFn: fetchWorkflows,
    retry: false,
  });
  const allRows = data?.workflows ?? EMPTY_WORKFLOWS;
  // Members have nothing to do about a workflow that is not set up, so only
  // the owner sees those, folded away from the ones that work.
  const rows = useMemo(
    () => (owner ? allRows : allRows.filter((row) => readiness(row) !== "setup")),
    [allRows, owner],
  );
  // Read the open workflow from the latest catalog so a refresh reaches it.
  const detail = allRows.find((row) => row.id === detailId) ?? null;
  const allCapabilities = [
    ...capabilityOrder,
    ...new Set(
      rows
        .map((row) => row.capability)
        .filter((cap) => !capabilityOrder.includes(cap as WorkflowCapability)),
    ),
  ];
  const availableCapabilities = allCapabilities.filter((cap) =>
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
  const working = filtered.filter((row) => readiness(row) !== "setup");
  const notSetUp = filtered.filter((row) => readiness(row) === "setup");
  const groupsOf = (list: WorkflowRow[]) =>
    outputGroups
      .map((output) => ({
        output,
        rows: list.filter((row) => outputGroup(row) === output),
      }))
      .filter((group) => group.rows.length > 0);
  const grouped = groupsOf(working);
  const setupGroups = groupsOf(notSetUp);

  return (
    <div className="space-y-(--space-section)">
      <PageHeader
        title="Workflows"
        count={data ? rows.length : undefined}
        subtitle="Choose by purpose and inputs."
        actions={
          data && isFetching ? (
            <Badge variant="outline">Refreshing</Badge>
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
          title={owner ? "No workflows are available." : "No workflows are available to you."}
          description={
            owner
              ? undefined
              : "Ask the owner what you can use."
          }
          action={
            <Button variant="outline" asChild>
              <Link prefetch={false} href="/models">Explore models</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <label className="relative min-w-0 flex-1 max-sm:basis-full sm:max-w-80">
              <Search
                aria-hidden="true"
                className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                size="sm"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search workflows"
                className="pl-9"
              />
              <span className="sr-only">Search workflows</span>
            </label>
            {/* A phone has no room for every task in a row, and a strip
                that scrolls hides the ones past the edge. A select lists
                them all (P4); wider screens keep the segments. */}
            <label className="block basis-full space-y-2 sm:hidden">
              <span className="text-sm font-medium">Task</span>
              <select
                className="border-input bg-card rounded-control min-h-11 w-full border px-3 text-base"
                value={capability}
                onChange={(event) => setCapability(event.target.value)}
              >
                <option value="all">All</option>
                {availableCapabilities.map((cap) => (
                  <option key={cap} value={cap}>
                    {capabilityLabel(cap)}
                  </option>
                ))}
              </select>
            </label>
            <Segmented
              label="Workflow task"
              className="max-sm:hidden"
              value={capability}
              onChange={setCapability}
              options={[
                { value: "all", label: "All", icon: SlidersHorizontal },
                ...availableCapabilities.map((cap) => ({
                  value: cap,
                  label: capabilityLabel(cap),
                })),
              ]}
            />
          </div>
          {filtered.length === 0 ? (
            <EmptyState
              icon={Search}
              title="No matching workflows"
              description="Clear the search or choose another task."
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
            <>
              {working.length > 0 && (
                <WorkflowGroups
                  label="Available workflows"
                  groups={grouped}
                  owner={owner}
                  onDetail={setDetailId}
                />
              )}
              {notSetUp.length > 0 && (
                <details
                  className="rounded-panel border p-4"
                  open={setupOpen || query.trim() !== ""}
                  onToggle={(event) => setSetupOpen(event.currentTarget.open)}
                >
                  <summary className="focus-visible:outline-ring min-h-8 max-sm:min-h-11 cursor-pointer py-1.5 font-medium focus-visible:outline-2">
                    Not set up ({notSetUp.length})
                  </summary>
                  <div className="mt-4">
                    <WorkflowGroups
                      label="Workflows that are not set up"
                      groups={setupGroups}
                      owner={owner}
                      onDetail={setDetailId}
                    />
                  </div>
                </details>
              )}
            </>
          )}
        </>
      )}
      <DetailSheet
        workflow={detail}
        owner={owner}
        onOpenChange={(open) => !open && setDetailId(null)}
      />
    </div>
  );
}

function WorkflowGroups({
  label,
  groups,
  owner,
  onDetail,
}: {
  label: string;
  groups: { output: string; rows: WorkflowRow[] }[];
  owner: boolean;
  onDetail: (id: string) => void;
}) {
  return (
    <div aria-label={label} className="space-y-(--space-section)">
      {groups.map((group) => (
        <section
          key={group.output}
          aria-label={`${group.output} workflows`}
          className="min-w-0"
        >
          <h2 className="text-heading mb-(--space-heading) flex items-baseline gap-2">
            {group.output}
            <span className="text-subtle-foreground text-meta figures">{group.rows.length}</span>
          </h2>
          <ListCard label={`${group.output} workflows`}>
            {group.rows.map((workflow) => (
              <WorkflowCard
                key={workflow.id}
                workflow={workflow}
                owner={owner}
                onDetail={(row) => onDetail(row.id)}
              />
            ))}
          </ListCard>
        </section>
      ))}
    </div>
  );
}
