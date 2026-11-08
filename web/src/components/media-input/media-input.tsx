"use client";

import { useRef } from "react";
import { Library, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatDuration, readAsDataUri, sizeWords, typeWords, type MediaTypes } from "@/lib/media-pick";
import { limitLabel } from "@/lib/library-file";
import { LibraryPicker } from "./library-picker";
import { PickedSummary, useObjectUrl } from "./picked-summary";
import { LIBRARY_SOURCE, isStrayClickAfterPick, useMediaPick, useTakeFocus } from "./use-media-pick";

function previewSource(value: string): string | null {
  // A Library asset id is not an address: its download route answers with a
  // link, not the bytes, so a pasted id has no preview.
  return /^data:(?:image|video)\//i.test(value) || /^https?:\/\//i.test(value)
    ? value
    : null;
}

/**
 * Picks a media input. It has two modes, one control either way:
 *
 * - **reference** (`value`/`onChange`): the media travels inside the request
 *   body as a data URI, a link or a Library asset id. The raw reference sits
 *   under Advanced.
 * - **file** (`file`/`onFileChange`): the media travels as a multipart file
 *   part. A Library item is downloaded into a file; there is no link box.
 *
 * A text box that asks for "value" is not a way to choose a picture, so the
 * main path is a file or the Library. The Library button shows when the Library
 * can hold the input's kind; an input that takes any file has none.
 */
export function MediaInput({
  id,
  label,
  kind,
  types,
  noun,
  accepted,
  hint,
  maxBytes,
  normalizeReference = (text) => text.trim(),
  value,
  file,
  invalid,
  required,
  describedBy,
  onChange,
  onFileChange,
}: {
  id: string;
  label: string;
  /** The kind of media taken ("image", "video", "audio"), or null for any file. */
  kind: string | null;
  /** The types taken when only some of the kind are ("image/png"); defaults to the whole kind. */
  types?: MediaTypes;
  noun?: string;
  accepted?: string;
  /** The line under the buttons while nothing is chosen; defaults to the size limit, when there is one. */
  hint?: string;
  /**
   * The largest file taken. A reference input holds the media inside the request, so it is held to the shared media
   * limit; a file input has no limit unless the screen passes one (the gateway's own limit answers for the rest).
   */
  maxBytes?: number;
  /** Reference mode: turns what was typed under Advanced into the stored reference (a pasted Library link into its id). */
  normalizeReference?: (text: string) => string;
  value?: string | undefined;
  file?: File | null;
  invalid?: boolean;
  /** The screen needs this input to run: the control to choose it says so to a screen reader. */
  required?: boolean;
  describedBy?: string;
  onChange?: (value: string | undefined) => void;
  onFileChange?: (file: File | null) => void;
}) {
  const fileMode = onFileChange !== undefined;
  const takes = types ?? (kind ? [`${kind}/*`] : undefined);
  const pick = useMediaPick({
    types: takes,
    kind,
    noun,
    accepted,
    maxBytes,
    current: fileMode ? (file ?? null) : undefined,
    currentReference: fileMode ? undefined : (value ?? null),
    // A reference input sends the media inside the request, so the file is read into a data URI first.
    prepare: fileMode ? undefined : readAsDataUri,
    commit: (picked, reference) => {
      if (fileMode) onFileChange?.(picked);
      else onChange?.(reference);
    },
  });
  const { noun: word } = pick;
  const fileUrl = useObjectUrl(fileMode ? file : null);
  const chosen = fileMode ? Boolean(file) : Boolean(value);
  const preview = fileMode ? fileUrl : value ? previewSource(value) : null;
  const dataUriChosen = !fileMode && Boolean(value?.startsWith("data:"));
  // The file the summary names: the chosen one, or in reference mode the file the reference was read from.
  const named = fileMode ? (file ?? null) : (pick.sourceFile);
  const drawn = named ? named.type.split("/")[0] : kind;
  const fromLibrary = pick.source === LIBRARY_SOURCE;
  const capitalWord = `${word[0].toUpperCase()}${word.slice(1)}`;
  const emptyHint = hint ?? (pick.maxBytes !== undefined ? `${capitalWord}s up to ${limitLabel(pick.maxBytes)}.` : null);
  const replaceButton = useRef<HTMLButtonElement>(null);
  const chooseButton = useRef<HTMLButtonElement>(null);
  useTakeFocus(pick, "replace", replaceButton);
  useTakeFocus(pick, "choose", chooseButton);

  function clear() {
    pick.clear();
    if (fileMode) onFileChange?.(null);
    else onChange?.(undefined);
  }

  return (
    <div className="space-y-3">
      {chosen ? (
        <PickedSummary
          key={fileMode ? "file" : "reference"}
          name={pick.sourceTitle ?? (named ? named.name : dataUriChosen ? `Chosen ${word}` : `${capitalWord} from a link`)}
          source={fromLibrary ? LIBRARY_SOURCE : null}
          facts={
            named
              ? [
                  typeWords(named.type),
                  pick.sourceSeconds && pick.sourceSeconds > 0 ? formatDuration(pick.sourceSeconds) : null,
                  sizeWords(named.size),
                ]
              : []
          }
          kind={drawn}
          previewUrl={preview}
          previewAlt={`Chosen ${word}`}
          retryLabel={`Retry preview of ${word} for ${label}`}
          invalid={invalid}
          replaceRef={replaceButton}
          onReplace={pick.replaceChosen}
          onRemove={clear}
          ignoreRemoveClick={isStrayClickAfterPick}
        />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button
              ref={chooseButton}
              id={id}
              type="button"
              variant="outline"
              size="sm"
              className="max-sm:h-11"
              aria-describedby={describedBy}
              aria-required={required || undefined}
              aria-invalid={invalid || undefined}
              onClick={pick.openFileDialog}
            >
              <Upload aria-hidden="true" />
              Choose a file
            </Button>
            {pick.libraryAvailable && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="max-sm:h-11"
                onClick={pick.openLibrary}
              >
                <Library aria-hidden="true" />
                Choose from Library
              </Button>
            )}
            {pick.pending && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="max-sm:h-11"
                aria-label={`Remove ${word} for ${label}`}
                // The second click of a double-click on a tile can land here while the download runs.
                onClick={(event) => {
                  if (!isStrayClickAfterPick(event)) clear();
                }}
              >
                <X aria-hidden="true" />
                Remove
              </Button>
            )}
          </div>
          {!pick.pending && emptyHint && (
            <p className="text-muted-foreground text-xs">{emptyHint}</p>
          )}
        </div>
      )}
      {pick.pending && (
        <p role="status" className="text-muted-foreground text-xs">
          Adding {word}…
          {pick.blockedSubmit && " Wait for it to finish, then run again."}
        </p>
      )}
      {pick.error && (
        <p role="alert" className="text-destructive text-xs">
          {pick.error}
        </p>
      )}
      <input
        ref={pick.fileInput}
        type="file"
        accept={pick.accept}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        data-media-file-input=""
        onChange={pick.onFileInputChange}
      />
      {!fileMode && (
        <details className="text-sm">
          <summary className="text-muted-foreground min-h-11 cursor-pointer py-3 text-xs">
            Advanced
          </summary>
          <label className="block space-y-1.5">
            <span className="text-xs font-medium">
              Link, Library link or data URI
            </span>
            <Input
              value={dataUriChosen ? "" : (value ?? "")}
              placeholder={
                dataUriChosen ? "Using the file you chose" : "https://…"
              }
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => {
                const next = normalizeReference(event.currentTarget.value);
                pick.replace();
                onChange?.(next || undefined);
              }}
            />
          </label>
        </details>
      )}
      {pick.libraryAvailable && pick.kind && (
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
      )}
    </div>
  );
}
