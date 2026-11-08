"use client";

import { useEffect, useId, useRef, useState, type DragEvent, type Ref } from "react";
import { Library, Paperclip, Upload, X } from "lucide-react";
import { LibraryPicker } from "@/components/media-input/library-picker";
import { FOCUS_RING } from "@/components/media-input/focus-ring";
import { PickedSummary, useObjectUrl } from "@/components/media-input/picked-summary";
import { LIBRARY_SOURCE, isStrayClickAfterPick, useMediaPick, useTakeFocus } from "@/components/media-input/use-media-pick";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/cn";
import { kindIcon } from "@/lib/media-kind";
import { formatDuration, sizeWords, typeWords } from "@/lib/media-pick";
import type { MediaInput } from "./home-model";
import { fitsMediaInput, mediaPhrase } from "./home-model";

/** A preview address for a chosen picture, which is all the chip draws. */
function usePreview(file: File | null, input: MediaInput): string | null {
  return useObjectUrl(input.modality === "image" ? file : null);
}

/** What the Library called a picked file: the chip above the words says it, as the drop zone's summary does. */
export type PickedFrom = { title?: string; seconds?: number | null };

/**
 * Takes a file handed to the composer from outside its controls (a paste), the way the dialog takes one: the type
 * check, the size check, and ending a Library pick still arriving. Each input registers its taker by modality.
 */
export type PasteTakers = { current: Record<string, ((file: File) => void) | undefined> };

/** The shared media-input behaviour, set up from a Home capability's declared input (its accept filter and word). */
function useHomePick(
  input: MediaInput,
  file: File | null,
  onFile: (file: File | null, from?: PickedFrom) => void,
  onPendingChange?: (pending: boolean) => void,
  takers?: PasteTakers,
) {
  const pick = useMediaPick({
    types: input.accept ? [input.accept] : undefined,
    noun: input.word,
    accepted: mediaPhrase(input),
    current: file,
    commit: (picked, _prepared, from) => onFile(picked, from),
    onPendingChange,
  });
  const take = pick.take;
  const modality = input.modality;
  // The latest take, registered for the composer's paste handler and dropped when this input goes.
  const latestTake = useRef(take);
  useEffect(() => {
    latestTake.current = take;
  });
  useEffect(() => {
    if (!takers) return;
    const registry = takers.current;
    const taker = (pasted: File) => void latestTake.current(pasted, { moveFocus: false });
    registry[modality] = taker;
    return () => {
      if (registry[modality] === taker) delete registry[modality];
    };
  }, [takers, modality]);
  return pick;
}

type HomePick = ReturnType<typeof useHomePick>;

function PickStatus({ pick, className }: { pick: HomePick; className?: string }) {
  return (
    <>
      {pick.pending ? (
        <p role="status" className={cn("text-muted-foreground text-meta", className)}>
          Adding {pick.noun}…
        </p>
      ) : null}
      {pick.error ? (
        <p role="alert" className={cn("text-destructive text-meta", className)}>
          {pick.error}
        </p>
      ) : null}
    </>
  );
}

function HomeLibraryPicker({ pick }: { pick: HomePick }) {
  if (!pick.libraryAvailable || !pick.kind) return null;
  return (
    <LibraryPicker
      open={pick.libraryOpen}
      onOpenChange={pick.setLibraryOpen}
      kind={pick.kind}
      noun={pick.noun}
      types={pick.types}
      currentId={pick.sourceAssetId}
      onPick={(item) => void pick.pickFromLibrary(item)}
      onChooseFile={pick.openFileDialog}
    />
  );
}

const PILL = cn(
  "border-border text-foreground text-meta hover:bg-muted inline-flex h-9 items-center gap-1.5 rounded-full border px-4 font-medium transition-colors duration-(--motion-fast) max-sm:min-h-11",
  FOCUS_RING,
);

/**
 * The composer's file input for one declared input modality (the way Midjourney, Runway and Meshy ask for a source
 * picture): a drop target that is also a button. Drop, choose, paste or take a file from the Library; on a phone the
 * file input's accept filter lets the system offer the camera. A chosen file shows as a thumbnail (a picture) or a
 * name, with a way to take it out. Nothing here names a kind of work: the modality and its accept filter are the data.
 */
export function HomeMediaInput({
  input,
  file,
  onFile,
  disabled = false,
  onPendingChange,
  takers,
  children,
}: {
  input: MediaInput;
  file: File | null;
  onFile: (file: File | null, from?: PickedFrom) => void;
  disabled?: boolean;
  /** Told while a Library pick is arriving, so Make waits for it. */
  onPendingChange?: (pending: boolean) => void;
  /** Where this input registers to take a pasted file. */
  takers?: PasteTakers;
  /** An extra control under the prompt line, such as a hand-off to record in Try. */
  children?: React.ReactNode;
}) {
  const inputId = useId();
  const [dragging, setDragging] = useState(false);
  const pick = useHomePick(input, file, onFile, onPendingChange, takers);
  const preview = useObjectUrl(file);
  const Icon = kindIcon(input.modality);
  const replaceButton = useRef<HTMLButtonElement>(null);
  const chooseButton = useRef<HTMLButtonElement>(null);
  useTakeFocus(pick, "replace", replaceButton);
  useTakeFocus(pick, "choose", chooseButton);

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (disabled) return;
    const dropped = Array.from(event.dataTransfer.files);
    void pick.take(dropped.find((candidate) => fitsMediaInput(candidate, input)) ?? dropped[0]);
  }

  return (
    <div
      data-slot="home-media-input"
      data-modality={input.modality}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        // The drop zone and the text box share one inner padding (COMPOSER_PAD), so their left edges line up.
        // Once a file is chosen the frame goes (it would make the row look empty), as on the Try pages.
        "mx-(--composer-pad) mt-(--composer-pad) mb-2 rounded-[calc(var(--radius-composer)-0.5rem)] border transition-colors duration-(--motion-fast)",
        file && !dragging ? "border-transparent px-0 py-1" : "border-dashed px-4 py-4",
        dragging ? "border-ring bg-accent" : file ? "" : "border-composer-edge-hover bg-muted/40",
      )}
    >
      <input
        ref={pick.fileInput}
        id={inputId}
        type="file"
        accept={pick.accept}
        disabled={disabled}
        className="sr-only"
        aria-label={`Choose ${mediaPhrase(input)}`}
        onChange={pick.onFileInputChange}
      />
      {file ? (
        <PickedSummary
          name={pick.sourceTitle ?? file.name}
          source={pick.source === LIBRARY_SOURCE ? LIBRARY_SOURCE : null}
          facts={[
            typeWords(file.type),
            pick.sourceSeconds && pick.sourceSeconds > 0 ? formatDuration(pick.sourceSeconds) : null,
            sizeWords(file.size),
          ]}
          kind={file.type.split("/")[0] || input.modality}
          previewUrl={preview}
          previewAlt={`Your ${input.word}: ${file.name}`}
          retryLabel={`Retry preview of ${file.name}`}
          replaceRef={replaceButton}
          onReplace={disabled ? undefined : pick.replaceChosen}
          ignoreRemoveClick={isStrayClickAfterPick}
          onRemove={() => {
            pick.clear();
            onFile(null);
          }}
        />
      ) : (
        <div className="flex flex-col items-center gap-2 py-3 text-center sm:py-4">
          <Icon aria-hidden="true" className="text-muted-foreground size-6" />
          <p className="text-muted-foreground max-sm:hidden">
            Drop {mediaPhrase(input)} here
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            <button ref={chooseButton} type="button" disabled={disabled} onClick={pick.openFileDialog} className={PILL}>
              <Upload aria-hidden="true" className="size-4" />
              Choose {mediaPhrase(input)}
            </button>
            {pick.libraryAvailable ? (
              <button type="button" disabled={disabled} onClick={pick.openLibrary} className={PILL}>
                <Library aria-hidden="true" className="size-4" />
                Choose from Library
              </button>
            ) : null}
          </div>
          {children}
        </div>
      )}
      <PickStatus pick={pick} className="mt-2" />
      <HomeLibraryPicker pick={pick} />
    </div>
  );
}

/**
 * The picked file as a chip above the text box, for a capability whose main input is words and whose file is the
 * second thing it takes (an edit): a thumbnail or icon, the name (the Library's title for an item taken from it),
 * and a way to take it out.
 */
export function PickedFileChip({
  input,
  file,
  title,
  onRemove,
}: {
  input: MediaInput;
  file: File;
  /** What the Library calls the file, when it came from there; otherwise the file's own name shows. */
  title?: string | null;
  onRemove: () => void;
}) {
  const preview = usePreview(file, input);
  const Icon = kindIcon(input.modality);
  const name = title ?? file.name;
  return (
    <div data-slot="home-picked-file" data-modality={input.modality} className="px-(--composer-pad) pt-3">
      <span className="bg-muted text-meta inline-flex max-w-full items-center gap-2 rounded-full py-1 pr-1 pl-1">
        {preview ? (
          // A local object address of the person's own picture: next/image has nothing to optimise.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={preview} alt={`Your ${input.word}: ${name}`} className="size-8 shrink-0 rounded-full object-cover" />
        ) : (
          <span className="text-muted-foreground flex size-8 shrink-0 items-center justify-center">
            <Icon aria-hidden="true" className="size-4" />
          </span>
        )}
        <span className="min-w-0 truncate font-medium">{name}</span>
        <button
          type="button"
          onClick={(event) => {
            if (!isStrayClickAfterPick(event)) onRemove();
          }}
          aria-label={`Remove ${name}`}
          className={cn(
            "text-muted-foreground hover:bg-border hover:text-foreground inline-flex size-10 shrink-0 cursor-pointer items-center justify-center rounded-full max-sm:size-11",
            FOCUS_RING,
          )}
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </span>
    </div>
  );
}

const ATTACH_CLASS = cn(
  "text-muted-foreground hover:bg-muted hover:text-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-full transition-colors duration-(--motion-fast) disabled:opacity-50 max-sm:size-11",
  FOCUS_RING,
);

/**
 * The attach button in the toolbar, for the same kind of capability. When the Library can hold the file it opens a
 * two-item menu (choose a file, choose from the Library); otherwise it opens the file dialog straight away. On a
 * phone the accept filter lets the system offer the camera. A file of the wrong kind is refused with a sentence.
 */
export function AttachFileButton({
  input,
  file,
  onFile,
  disabled = false,
  onPendingChange,
  takers,
  buttonRef,
}: {
  input: MediaInput;
  file: File | null;
  onFile: (file: File | null, from?: PickedFrom) => void;
  disabled?: boolean;
  /** Told while a Library pick is arriving, so Make waits for it. */
  onPendingChange?: (pending: boolean) => void;
  /** Where this input registers to take a pasted file. */
  takers?: PasteTakers;
  /** The attach button, so the composer can move focus to it when the chip is removed. */
  buttonRef?: Ref<HTMLButtonElement>;
}) {
  const pick = useHomePick(input, file, onFile, onPendingChange, takers);
  const attachButton = useRef<HTMLButtonElement | null>(null);
  useTakeFocus(pick, "replace", attachButton);
  const setButton = (node: HTMLButtonElement | null) => {
    attachButton.current = node;
    if (typeof buttonRef === "function") buttonRef(node);
    else if (buttonRef) (buttonRef as { current: HTMLButtonElement | null }).current = node;
  };
  const label = file ? `Change ${input.word}` : `Attach ${mediaPhrase(input)}`;
  return (
    <>
      <input
        ref={pick.fileInput}
        type="file"
        accept={pick.accept}
        disabled={disabled}
        className="sr-only"
        tabIndex={-1}
        aria-label={`Choose ${mediaPhrase(input)}`}
        onChange={pick.onFileInputChange}
      />
      {pick.libraryAvailable ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button ref={setButton} type="button" disabled={disabled} aria-label={label} title={label} className={ATTACH_CLASS}>
              <Paperclip aria-hidden="true" className="size-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem onSelect={pick.openFileDialog}>
              <Upload aria-hidden="true" />
              Choose {mediaPhrase(input)}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={pick.openLibrary}>
              <Library aria-hidden="true" />
              Choose from Library
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <button ref={setButton} type="button" disabled={disabled} onClick={pick.openFileDialog} aria-label={label} title={label} className={ATTACH_CLASS}>
          <Paperclip aria-hidden="true" className="size-4" />
        </button>
      )}
      <PickStatus pick={pick} className="basis-full" />
      <HomeLibraryPicker pick={pick} />
    </>
  );
}
