"use client";

import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Segmented, type SegmentOption } from "@/components/product/segmented";
import { productApi, type ModelsCatalog } from "@/lib/product-api";
import { modelDisplayName, prettifyModelId } from "@/lib/model-name";
import type { CapabilityStateKey, CapabilityView } from "@/lib/capability-state";
import type { HomeCapability } from "./home-api";
import { storeHomeHandoff } from "./handoff";
import {
  capabilityIs,
  capabilityLabel,
  kindOf,
  makeVerb,
  needsRecording,
  promptPlaceholder,
  runFacts,
  tryHasOwnTab,
} from "./home-model";

/** The states a person can still make something in. Off, down and blocked cannot run. */
const USABLE = new Set(["ready", "busy", "degraded", "unknown"]);
const CHOOSER_SHOWN = 5;

type CatalogRow = ModelsCatalog["models"][number];

/** A catalog row's facts, in the shape a capability row carries, so one line reads both. */
function factsFromCatalog(row: CatalogRow, base: HomeCapability): HomeCapability {
  const price = row.cost?.price;
  return {
    ...base,
    typical_seconds: row.timing?.typical_seconds ?? null,
    cost_hint: {
      kind: row.cost?.kind ?? "unknown",
      price_usd: typeof price === "number" ? price : null,
    },
  };
}

/** The message above the composer when the chosen capability cannot run: one fact, one next step. */
function stateNotice(
  row: HomeCapability,
  view: CapabilityView | undefined,
  owner: boolean,
  ownerName: string | null,
): { text: string; action: { label: string; href: string } | null } | null {
  if (!view || USABLE.has(view.state)) return null;
  const label = capabilityLabel(row);
  const who = ownerName ?? "the gateway owner";
  if (owner) {
    if (view.state === "blocked")
      return { text: `${capabilityIs(label, "not in your access plan")}.`, action: { label: "Open People", href: "/people?tab=plans" } };
    return {
      text: `${capabilityIs(label, view.word.toLowerCase())}. ${view.detail ?? "Check it in Engines."}`,
      action: { label: "Open Engines", href: "/engines/" },
    };
  }
  if (view.state === "blocked")
    return { text: `${capabilityIs(label, "not in your access plan")}. Ask ${who} to change it.`, action: null };
  if (view.state === "down")
    return { text: `${capabilityIs(label, "down")}. Ask ${who} to look at it.`, action: null };
  return { text: `${capabilityIs(label, "turned off")}. Ask ${who} to turn it back on.`, action: null };
}

/** Whether a row scrolls sideways and has more out of view on its right; the edge fade shows only then. */
function useMoreToRight(ref: RefObject<HTMLElement | null>, count: number): boolean {
  const [more, setMore] = useState(false);
  useEffect(() => {
    const row = ref.current;
    if (!row) return;
    const check = () => setMore(row.scrollWidth - row.clientWidth - row.scrollLeft > 1);
    check();
    row.addEventListener("scroll", check, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(check);
    observer?.observe(row);
    return () => {
      row.removeEventListener("scroll", check);
      observer?.disconnect();
    };
  }, [ref, count]);
  return more;
}

/** A capability that is not working: its word in muted type, with a small dot. It never boxes in the name. */
function StateNote({ state, word }: { state: CapabilityStateKey; word: string }) {
  return (
    <span className="text-muted-foreground inline-flex items-center gap-1.5 font-normal">
      <span
        aria-hidden="true"
        className={cn("size-1.5 shrink-0 rounded-full", state === "down" ? "bg-destructive" : "bg-muted-foreground/60")}
      />
      {word}
    </span>
  );
}

export function ChooserSkeleton() {
  return (
    <div role="status" aria-label="Loading what you can make" aria-busy="true" className="flex min-w-0 gap-1 overflow-hidden">
      {[0, 1, 2, 3, 4].map((item) => (
        <Skeleton key={item} className="rounded-control h-8 w-24 shrink-0 motion-reduce:animate-none" />
      ))}
    </div>
  );
}

/**
 * The chooser and the composer. Every option, model name, cost and wait comes from the rows
 * `/me/api/home` returned, plus the catalog when the model picker opens (or a non-default model is
 * chosen). Make hands the work to Try: a prompt travels in sessionStorage, never in the URL.
 */
export function HomeComposer({
  rows,
  views,
  loading,
  owner,
  ownerName,
  paidOff,
  kind: chosenKind,
  onKindChange,
}: {
  rows: HomeCapability[];
  views: CapabilityView[];
  loading: boolean;
  owner: boolean;
  ownerName: string | null;
  /** The spend cap is reached: paid models cannot run, and free ones still can. */
  paidOff: boolean;
  /** The kind of work chosen (the page owns the choice, so a list row can choose it too). */
  kind: string | null;
  onKindChange: (kind: string) => void;
}) {
  const router = useRouter();
  const viewByKind = new Map(views.map((view) => [view.key, view]));
  const usableRow = rows.find((row) => USABLE.has(viewByKind.get(kindOf(row))?.state ?? "ready"));
  const selected = rows.find((row) => kindOf(row) === chosenKind) ?? usableRow ?? rows[0] ?? null;
  const kind = selected ? kindOf(selected) : "";
  const recommended = selected?.use_name ?? selected?.recommended_model ?? selected?.title ?? null;
  const [modelChoice, setModelChoice] = useState<Record<string, string>>({});
  const model = (kind && modelChoice[kind]) || recommended;
  const [prompt, setPrompt] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const usesCatalog = pickerOpen || (model !== null && model !== recommended);
  const catalog = useQuery({
    queryKey: ["home", "model-catalog"],
    queryFn: () => productApi.models(),
    enabled: usesCatalog && !loading,
    staleTime: 60_000,
  });
  const catalogRows: CatalogRow[] = [...(catalog.data?.models ?? []), ...(catalog.data?.aliases ?? [])];
  const catalogRowFor = (name: string | null) =>
    name ? catalogRows.find((row) => row.id === name || row.use_names?.includes(name)) ?? null : null;


  const view = viewByKind.get(kind);
  const notice = selected ? stateNotice(selected, view, owner, ownerName) : null;
  const facts = (() => {
    if (!selected || !model) return null;
    if (model === recommended) return selected;
    const entry = catalogRowFor(model);
    return entry ? factsFromCatalog(entry, selected) : null;
  })();
  const factsLine = facts ? runFacts(facts) : "";
  const cost = facts?.cost_hint?.kind;
  const runnable = !loading && selected !== null && USABLE.has(view?.state ?? "ready") && !(paidOff && cost === "paid");
  const verb = makeVerb(kind);
  const recording = needsRecording(kind);
  const canMake = runnable && (recording || prompt.trim().length > 0) && model !== null;

  const options: SegmentOption<string>[] = rows.map((row) => {
    const state = viewByKind.get(kindOf(row));
    return {
      value: kindOf(row),
      label: capabilityLabel(row),
      badge: state && !USABLE.has(state.state) ? <StateNote state={state.state} word={state.word} /> : null,
    };
  });
  const shown = options.slice(0, CHOOSER_SHOWN);
  const more = options.slice(CHOOSER_SHOWN);
  const chooserRef = useRef<HTMLDivElement>(null);
  const moreToRight = useMoreToRight(chooserRef, shown.length);

  function startWork(event?: FormEvent) {
    event?.preventDefault();
    if (!canMake || !model || !selected) return;
    const text = recording ? "" : prompt.trim();
    storeHomeHandoff({ capability: kind, model, prompt: text });
    const params = new URLSearchParams({ capability: kind, model });
    // Try runs a kind without a tab of its own through its workflow, so the chosen workflow must travel too.
    const workflow = model === recommended ? (selected.workflow ?? null) : (catalogRowFor(model)?.workflow ?? null);
    if (workflow && !tryHasOwnTab(kind)) params.set("workflow", workflow);
    router.push(`/try/?${params.toString()}`);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) startWork();
  }

  const pickerNames = selected
    ? [...new Set([recommended, ...(selected.use_names ?? [])].filter((name): name is string => Boolean(name)))]
    : [];
  const nameOf = (name: string) => {
    if (selected && name === recommended && selected.title) return selected.title;
    const entry = catalogRowFor(name);
    return entry ? modelDisplayName(name, { models: [entry], aliases: [] }) : prettifyModelId(name);
  };
  const placeholder = promptPlaceholder(kind);

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex min-w-0 items-center gap-2">
        {loading || !selected ? (
          <ChooserSkeleton />
        ) : (
          <>
            {/* On a phone the row scrolls inside itself; the fade on its trailing edge says there is more. */}
            <div className="relative flex min-w-0">
              <Segmented<string>
                ref={chooserRef}
                label="What to make"
                value={kind}
                options={shown}
                onChange={onKindChange}
                scroll
              />
              {moreToRight ? (
                <span
                  aria-hidden="true"
                  data-slot="chooser-fade"
                  className="from-background pointer-events-none absolute inset-y-0 right-0 w-10 bg-linear-to-l to-transparent"
                />
              ) : null}
            </div>
            {more.length ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="sm" className="shrink-0">
                    More
                    <ChevronDown aria-hidden="true" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuRadioGroup value={kind} onValueChange={onKindChange}>
                    {more.map((option) => (
                      <DropdownMenuRadioItem key={option.value} value={option.value}>
                        {option.label}
                        {option.badge}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </>
        )}
      </div>
      <ComposerFrame>
        {notice ? (
          <p role="status" className="text-meta flex flex-wrap items-center gap-x-2 gap-y-1 px-3 pt-3">
            <span>{notice.text}</span>
            {notice.action ? (
              <Button asChild variant="outline" size="sm">
                <Link prefetch={false} href={notice.action.href}>{notice.action.label}</Link>
              </Button>
            ) : null}
          </p>
        ) : null}
        {recording ? (
          <div className="space-y-3 px-3 pt-3">
            <p className="text-meta">Transcription works on a recording. Open Try to choose one.</p>
          </div>
        ) : (
          <>
            <label htmlFor="home-prompt" className="sr-only">
              {placeholder}
            </label>
            {/* One box for the whole page: it keeps what was typed while the list loads. */}
            <Textarea
              id="home-prompt"
              rows={3}
              placeholder={placeholder}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={onKeyDown}
              className="min-h-24 resize-none border-0 px-3 pt-3 shadow-none focus-visible:ring-0"
            />
          </>
        )}
        <form onSubmit={startWork} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 pb-3">
          {selected ? (
            <DropdownMenu onOpenChange={setPickerOpen}>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="max-w-[60%] min-w-0 justify-start">
                  <span className="truncate">{model ? nameOf(model) : ""}</span>
                  <ChevronDown aria-hidden="true" className="shrink-0" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="max-h-80 overflow-y-auto">
                <DropdownMenuRadioGroup
                  value={model ?? ""}
                  onValueChange={(value) => setModelChoice((current) => ({ ...current, [kind]: value }))}
                >
                  {pickerNames.map((name) => (
                    <DropdownMenuRadioItem key={name} value={name}>
                      {nameOf(name)}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <Skeleton className="h-8 w-28 motion-reduce:animate-none" />
          )}
          {factsLine ? (
            <p className={cn("text-muted-foreground text-meta min-w-0", "max-sm:order-last max-sm:w-full")}>{factsLine}</p>
          ) : null}
          {recording ? (
            <Button type="button" className="ml-auto" disabled={!runnable || !model} onClick={() => startWork()}>
              Open in Try
            </Button>
          ) : (
            <Button type="submit" className="ml-auto" disabled={!canMake} aria-label={verb}>
              {verb}
            </Button>
          )}
        </form>
      </ComposerFrame>
    </div>
  );
}

function ComposerFrame({ children }: { children: ReactNode }) {
  return (
    <div className="bg-card border-input rounded-control max-w-[760px] border">
      {children}
    </div>
  );
}
