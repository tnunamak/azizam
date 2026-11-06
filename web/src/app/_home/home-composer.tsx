"use client";

import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { ArrowUp, ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";
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
import { capabilityMeta } from "@/components/product/readiness";
import { productApi, type ModelsCatalog } from "@/lib/product-api";
import { modelDisplayName, prettifyModelId } from "@/lib/model-name";
import type { CapabilityStateKey, CapabilityView } from "@/lib/capability-state";
import type { HomeCapability } from "./home-api";
import { rememberMade, storeHomeHandoff } from "./handoff";
import { AttachFileButton, HomeMediaInput, PickedFileChip, type PasteTakers, type PickedFrom } from "./home-media-input";
import {
  capabilityIs,
  capabilityLabel,
  composerInputs,
  mediaPhrase,
  fitsMediaInput,
  kindOf,
  actionLabel,
  isRecordable,
  promptPlaceholder,
  runFacts,
  tryHasOwnTab,
  wordsToMediaRow,
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

/**
 * A capability that is not working: a small dot after its name, and its word for screen readers. Choosing
 * it explains the fault in the composer. The owner sees no dot: the gateway panel (or, below 1024 px, its
 * one-row banner) is the one place the fault shows for them.
 */
function StateNote({ state, word, ownerQuiet }: { state: CapabilityStateKey; word: string; ownerQuiet: boolean }) {
  return (
    // Relative, so the word for screen readers stays inside the chooser's scroll box and never widens the page.
    <span className="relative inline-flex items-center">
      {ownerQuiet ? null : (
        <span
          aria-hidden="true"
          className={cn("size-1.5 shrink-0 rounded-full", state === "down" ? "bg-destructive" : "bg-muted-foreground/60")}
        />
      )}
      <span className="sr-only">{word}</span>
    </span>
  );
}

/** The row the composer works with: the one chosen, else the first that can run now, else the first. */
export function selectedRow(rows: HomeCapability[], views: CapabilityView[], chosen: string | null): HomeCapability | null {
  const viewByKind = new Map(views.map((view) => [view.key, view]));
  const usableRow = rows.find((row) => USABLE.has(viewByKind.get(kindOf(row))?.state ?? "ready"));
  return rows.find((row) => kindOf(row) === chosen) ?? usableRow ?? rows[0] ?? null;
}

export function ChooserSkeleton() {
  return (
    <div role="status" aria-label="Loading what you can make" aria-busy="true" className="flex min-w-0 gap-2 overflow-hidden sm:justify-center">
      {[0, 1, 2, 3, 4].map((item) => (
        <Skeleton key={item} className="h-8 w-23 shrink-0 rounded-full motion-reduce:animate-none max-sm:h-11" />
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
  chooser = true,
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
  /** Whether the chips under the box show. A first visit's cards are the chooser instead. */
  chooser?: boolean;
}) {
  const router = useRouter();
  const viewByKind = new Map(views.map((view) => [view.key, view]));
  const selected = selectedRow(rows, views, chosenKind);
  const kind = selected ? kindOf(selected) : "";
  const recommended = selected?.use_name ?? selected?.recommended_model ?? selected?.title ?? null;
  const [modelChoice, setModelChoice] = useState<Record<string, string>>({});
  const model = (kind && modelChoice[kind]) || recommended;
  const [prompt, setPrompt] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const usesCatalog = pickerOpen || (model !== null && model !== recommended);
  const { data: catalogData } = useQuery({
    queryKey: ["home", "model-catalog"],
    queryFn: () => productApi.models(),
    enabled: usesCatalog && !loading,
    staleTime: 60_000,
  });
  const catalogRows: CatalogRow[] = [...(catalogData?.models ?? []), ...(catalogData?.aliases ?? [])];
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
  const verb = actionLabel(selected);
  // What the chosen capability asks for comes from its declared input modalities: text, and a file for each
  // modality a request must carry. Files are kept per kind of work, so choosing another kind and back keeps them.
  const inputs = composerInputs(selected);
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const fileFor = (modality: string) => files[`${kind}:${modality}`] ?? null;
  // What the Library called a file picked from it; it names the chip only while that is still the file held.
  const [fromLibrary, setFromLibrary] = useState<Record<string, { file: File; title: string }>>({});
  const titleFor = (modality: string) => {
    const entry = fromLibrary[`${kind}:${modality}`];
    return entry && entry.file === fileFor(modality) ? entry.title : null;
  };
  /** A file chosen by the input's own controls (the dialog, a drop, the Library), with what the Library called it. */
  const chooseFile = (modality: string) => (file: File | null, from?: PickedFrom) => {
    const key = `${kind}:${modality}`;
    setFiles((current) => ({ ...current, [key]: file }));
    setFromLibrary((current) => {
      const next = { ...current };
      if (file && from?.title) next[key] = { file, title: from.title };
      else delete next[key];
      return next;
    });
  };
  // Each file input registers how to take a pasted file, and the attach buttons how to be focused, by modality.
  const takers = useRef<PasteTakers["current"]>({});
  const attachButtons = useRef<Record<string, HTMLButtonElement | null>>({});
  const missingInput = inputs.media.find((input) => !fileFor(input.modality)) ?? null;
  // A Library pick is still arriving for one of the inputs: Make waits, as it would for a missing file.
  const [arriving, setArriving] = useState<Record<string, boolean>>({});
  const pickArriving = Object.values(arriving).some(Boolean);
  const arrivingFor = (input: { modality: string }) => (pending: boolean) =>
    setArriving((current) =>
      current[`${kind}:${input.modality}`] === pending ? current : { ...current, [`${kind}:${input.modality}`]: pending },
    );
  // When words are the main input and a file is the second thing it takes (an edit), the file is a chip above the
  // words with an attach button in the toolbar. The large drop zone is for a capability whose inputs have no words.
  const filesBesideWords = inputs.text === "required" && inputs.media.length > 0;
  const textReady = inputs.text !== "required" || prompt.trim().length > 0;
  const canMake = runnable && missingInput === null && !pickArriving && textReady && model !== null;
  // Why Make waits, said where the person looks and on the button itself: the first input the capability needs.
  const waitingFor = missingInput ? `Add ${mediaPhrase(missingInput)} to start` : !textReady ? "Write what you want to start" : null;
  // A capability that needs a picture can start from words: they become a picture first, in Try, when a capability
  // that makes pictures from words is usable. The offer shows only while the words are typed and no picture is chosen.
  const needsImage = inputs.media.some((input) => input.modality === "image");
  const pictureRow = needsImage ? wordsToMediaRow(rows, "image") : null;
  const pictureKind = pictureRow ? kindOf(pictureRow) : null;
  const pictureModel = pictureRow ? (pictureRow.use_name ?? pictureRow.recommended_model ?? pictureRow.title ?? null) : null;
  const pictureReady =
    pictureRow !== null &&
    pictureModel !== null &&
    USABLE.has(viewByKind.get(pictureKind ?? "")?.state ?? "ready") &&
    !(paidOff && pictureRow.cost_hint?.kind === "paid");
  const offerPictureFirst =
    inputs.text !== "none" && needsImage && !fileFor("image") && pictureReady && prompt.trim().length > 0;

  const options: SegmentOption<string>[] = rows.map((row) => {
    const state = viewByKind.get(kindOf(row));
    return {
      value: kindOf(row),
      label: capabilityLabel(row),
      icon: capabilityMeta(kindOf(row), row.output_modalities).icon,
      badge: state && !USABLE.has(state.state) ? <StateNote state={state.state} word={state.word} ownerQuiet={owner} /> : null,
    };
  });
  const shown = options.slice(0, CHOOSER_SHOWN);
  const more = options.slice(CHOOSER_SHOWN);
  const chooserRef = useRef<HTMLDivElement>(null);
  const moreToRight = useMoreToRight(chooserRef, shown.length);

  function pushToTry(handoffKind: string, handoffModel: string, workflow: string | null) {
    const params = new URLSearchParams({ capability: handoffKind, model: handoffModel });
    // Try runs a kind without a tab of its own through its workflow, so the chosen workflow must travel too.
    if (workflow && !tryHasOwnTab(handoffKind)) params.set("workflow", workflow);
    router.push(`/try/?${params.toString()}`);
  }

  function startWork(event?: FormEvent) {
    event?.preventDefault();
    if (!canMake || !model || !selected) return;
    const text = inputs.text === "none" ? "" : prompt.trim();
    const chosen = inputs.media.map((input) => fileFor(input.modality)).filter((file): file is File => file !== null);
    storeHomeHandoff({ capability: kind, model, prompt: text, files: chosen });
    if (text) rememberMade({ capability: kind, prompt: text });
    pushToTry(kind, model, model === recommended ? (selected.workflow ?? null) : (catalogRowFor(model)?.workflow ?? null));
  }

  /** Opens Try on this kind of work with no file, for what Home cannot take (a recording made on the spot). */
  function openInTry() {
    if (!runnable || !model || !selected) return;
    storeHomeHandoff({ capability: kind, model, prompt: "" });
    pushToTry(kind, model, model === recommended ? (selected.workflow ?? null) : (catalogRowFor(model)?.workflow ?? null));
  }

  // A pasted file (a screenshot, a copied picture) goes to the first file input it fits that is still empty, so a
  // picture never needs the file dialog. Pasted words are left alone.
  function onPaste(event: ClipboardEvent<HTMLDivElement>) {
    const pasted = Array.from(event.clipboardData.files);
    if (!pasted.length || !runnable) return;
    for (const input of inputs.media) {
      const match = fileFor(input.modality) ? undefined : pasted.find((file) => fitsMediaInput(file, input));
      if (!match) continue;
      event.preventDefault();
      // Through the input's own pick, so a paste is checked as a chosen file is and ends a Library pick still arriving.
      const take = takers.current[input.modality];
      if (take) take(match);
      else chooseFile(input.modality)(match);
      return;
    }
  }

  function makePictureFirst() {
    const text = prompt.trim();
    if (!pictureReady || !pictureRow || !pictureKind || !pictureModel || !text) return;
    storeHomeHandoff({ capability: pictureKind, model: pictureModel, prompt: text });
    rememberMade({ capability: pictureKind, prompt: text });
    pushToTry(pictureKind, pictureModel, pictureRow.workflow ?? null);
  }

  // Enter makes; Shift+Enter starts a new line. A key still being composed (an IME's) is left alone. Safari sends
  // the Enter that confirms an IME candidate after compositionend, with isComposing false and keyCode 229.
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    event.preventDefault();
    startWork();
  }

  const pickerNames = selected
    ? [...new Set([recommended, ...(selected.use_names ?? [])].filter((name): name is string => Boolean(name)))]
    : [];
  const nameOf = (name: string) => {
    if (selected && name === recommended && selected.title) return selected.title;
    const entry = catalogRowFor(name);
    return entry ? modelDisplayName(name, { models: [entry], aliases: [] }) : prettifyModelId(name);
  };
  const placeholder =
    inputs.media.length === 0 ? promptPlaceholder(selected) : inputs.text === "optional" ? "Add a note (optional)" : promptPlaceholder(selected);

  const chips = loading || !selected ? (
    <ChooserSkeleton />
  ) : (
    // On a phone the row bleeds to the screen's edges, so a chip is cut by the screen, never by the page's
    // padding, and it scrolls chip by chip; the fade on its trailing edge says there is more.
    <div className="flex min-w-0 items-center gap-2 max-sm:-mx-(--page-pad) sm:justify-center">
      <div className="relative flex min-w-0">
        <Segmented<string>
          ref={chooserRef}
          label="What to make"
          value={kind}
          options={shown}
          onChange={onKindChange}
          scroll
          pill
          className="max-sm:scroll-px-(--page-pad) max-sm:px-(--page-pad)"
        />
        {moreToRight ? (
          <span
            aria-hidden="true"
            data-slot="chooser-fade"
            className="from-background pointer-events-none absolute inset-y-0 right-0 w-6 bg-linear-to-l to-transparent"
          />
        ) : null}
      </div>
      {more.length ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className={cn(CHIP, "text-muted-foreground")}>
              More
              <ChevronDown aria-hidden="true" className="size-4" />
            </button>
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
    </div>
  );

  return (
    <div className="mx-auto w-full max-w-(--content-composer) min-w-0 space-y-3">
      <ComposerFrame onPaste={onPaste}>
        {notice ? (
          <p role="status" className="text-meta flex flex-wrap items-center gap-x-3 gap-y-2 px-5 pt-4">
            <span>{notice.text}</span>
            {notice.action ? (
              <Link
                prefetch={false}
                href={notice.action.href}
                className="text-link inline-flex items-center font-medium underline-offset-4 hover:underline max-sm:min-h-11"
              >
                {notice.action.label}
              </Link>
            ) : null}
          </p>
        ) : null}
        {filesBesideWords
          ? inputs.media.map((input) => {
              const picked = fileFor(input.modality);
              return picked ? (
                <PickedFileChip
                  key={`${kind}:${input.modality}`}
                  input={input}
                  file={picked}
                  title={titleFor(input.modality)}
                  onRemove={() => {
                    chooseFile(input.modality)(null);
                    // The chip is gone; focus goes to the attach button, the way to choose again.
                    attachButtons.current[input.modality]?.focus();
                  }}
                />
              ) : null;
            })
          : inputs.media.map((input) => (
          <HomeMediaInput
            key={`${kind}:${input.modality}`}
            input={input}
            file={fileFor(input.modality)}
            onFile={chooseFile(input.modality)}
            disabled={!runnable}
            onPendingChange={arrivingFor(input)}
            takers={takers}
          >
            {isRecordable(selected, input.modality) ? (
              <button
                type="button"
                onClick={openInTry}
                disabled={!runnable}
                className="text-link text-meta inline-flex items-center font-medium underline-offset-4 hover:underline max-sm:min-h-11"
              >
                Or record in Try
              </button>
            ) : null}
          </HomeMediaInput>
        ))}
        {inputs.text !== "none" ? (
          <>
            <label htmlFor="home-prompt" className="sr-only">
              {placeholder}
            </label>
            {/* One box for the whole page: it keeps what was typed while the list loads. */}
            <Textarea
              id="home-prompt"
              rows={inputs.media.length ? 2 : 3}
              placeholder={placeholder}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={onKeyDown}
              className={cn(
                // The card shows the focus edge; the box adds none of its own, so nothing overlaps the card's border.
                "text-heading placeholder:text-muted-foreground resize-none border-0 bg-transparent px-(--composer-pad) pt-4 pb-1 font-normal shadow-none focus-visible:border-transparent! focus-visible:ring-0!",
                inputs.media.length ? "min-h-16" : "min-h-24 sm:min-h-26",
              )}
            />
          </>
        ) : null}
        {offerPictureFirst ? (
          <div className="text-meta text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-2 px-5 pb-3">
            <button
              type="button"
              className="text-link inline-flex items-center font-medium underline-offset-4 hover:underline max-sm:min-h-11"
              onClick={makePictureFirst}
            >
              Make a picture of this first
            </button>
          </div>
        ) : null}
        <form onSubmit={startWork} className="flex items-center gap-2 px-3 pb-3">
          {/* The model and what a run costs read as one group; on a phone the facts sit under the model. */}
          <div className="flex min-w-0 flex-1 flex-col items-start gap-1 sm:flex-row sm:items-center sm:gap-3">
            {/* The attach button and the model chip share a line, so a phone does not give the button a row of its own. */}
            <div className="flex max-w-full min-w-0 items-center gap-2 max-sm:flex-wrap sm:max-w-[60%]">
            {filesBesideWords
              ? inputs.media.map((input) => (
                  <AttachFileButton
                    key={`${kind}:${input.modality}`}
                    input={input}
                    file={fileFor(input.modality)}
                    onFile={chooseFile(input.modality)}
                    disabled={!runnable}
                    onPendingChange={arrivingFor(input)}
                    takers={takers}
                    buttonRef={(node) => {
                      attachButtons.current[input.modality] = node;
                    }}
                  />
                ))
              : null}
            {selected ? (
              <DropdownMenu onOpenChange={setPickerOpen}>
                <DropdownMenuTrigger asChild>
                  <button type="button" className={cn(CHIP, "bg-muted hover:bg-accent max-w-full min-w-0 border-transparent")}>
                    <span className="truncate">{model ? nameOf(model) : ""}</span>
                    <ChevronDown aria-hidden="true" className="text-muted-foreground size-4 shrink-0" />
                  </button>
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
              <Skeleton className="h-8 w-28 rounded-full motion-reduce:animate-none" />
            )}
            </div>
            {runnable && missingInput ? (
              <p className="text-muted-foreground text-meta min-w-0 truncate max-sm:px-1">{waitingFor}</p>
            ) : factsLine ? (
              <p className="text-muted-foreground text-meta min-w-0 truncate max-sm:px-1">{factsLine}</p>
            ) : null}
          </div>
          <button
            type="submit"
            data-slot="button"
            data-variant="default"
            aria-disabled={!canMake}
            title={!canMake && runnable ? (waitingFor ?? undefined) : undefined}
            onClick={(event) => {
              if (!canMake) event.preventDefault();
            }}
            className={cn(MAKE, canMake ? MAKE_READY : MAKE_RESTING)}
          >
            {verb}
            <ArrowUp aria-hidden="true" className="size-4" />
          </button>
        </form>
      </ComposerFrame>
      {chooser ? chips : null}
    </div>
  );
}

/** A pill in the composer's world: the model chip, the chooser's More and (in Segmented) the chooser. */
const CHIP =
  "border-border text-foreground text-meta hover:bg-muted inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full border px-3 font-medium whitespace-nowrap transition-colors duration-(--motion-fast) max-sm:min-h-11";

/**
 * Make is always findable and says what it will do. While a required input is missing it reads as unavailable
 * (muted, not-allowed cursor, aria-disabled so it stays focusable and says why on hover and in the line beside
 * it), then takes the action colour once there is something to make.
 */
const MAKE =
  "focus-visible:ring-ring inline-flex h-10 shrink-0 items-center justify-center gap-1.5 rounded-full px-4 text-meta font-semibold whitespace-nowrap transition-colors duration-(--motion-fast) focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none max-sm:min-h-11 xl:h-9";
const MAKE_RESTING = "bg-muted text-muted-foreground cursor-not-allowed";
const MAKE_READY = "bg-make text-make-foreground hover:bg-make/90 cursor-pointer";

/**
 * The page's one lifted object (DESIGN "Composer"). Light: a hairline and a soft shadow, the 3:1 control
 * edge on hover. Dark: a surface 5% lighter than the page, with no edge. Both show the focus colour while
 * someone is typing in it.
 */
function ComposerFrame({ children, onPaste }: { children: ReactNode; onPaste?: (event: ClipboardEvent<HTMLDivElement>) => void }) {
  return (
    <div
      data-slot="composer"
      onPaste={onPaste}
      className="bg-composer border-composer-edge hover:border-composer-edge-hover focus-within:border-ring! rounded-composer shadow-composer w-full min-w-0 border transition-colors duration-(--motion-fast)"
    >
      {children}
    </div>
  );
}
