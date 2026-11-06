"use client";

import { useEffect, useState } from "react";
import { Library, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { kindIcon } from "@/lib/media-kind";
import { MAX_MEDIA_BYTES, readAsDataUri, type MediaTypes } from "@/lib/media-pick";
import { limitLabel } from "@/lib/library-file";
import { LibraryPicker } from "./library-picker";
import { useMediaPick } from "./use-media-pick";

function previewSource(value: string): string | null {
  // A Library asset id is not an address: its download route answers with a
  // link, not the bytes, so a pasted id has no preview.
  return /^data:(?:image|video)\//i.test(value) || /^https?:\/\//i.test(value)
    ? value
    : null;
}

/** A preview link for a chosen file, revoked when the file changes. */
function useObjectUrl(file: File | null | undefined): string | null {
  const [objectUrl, setObjectUrl] = useState<{
    file: File;
    url: string;
  } | null>(null);
  useEffect(() => {
    if (!file || typeof URL.createObjectURL !== "function") {
      return;
    }
    const next = URL.createObjectURL(file);
    setObjectUrl({ file, url: next });
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return objectUrl && objectUrl.file === file ? objectUrl.url : null;
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
  maxBytes = MAX_MEDIA_BYTES,
  normalizeReference = (text) => text.trim(),
  value,
  file,
  invalid,
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
  /** The line under the buttons while nothing is chosen; defaults to the size limit. */
  hint?: string;
  maxBytes?: number;
  /** Reference mode: turns what was typed under Advanced into the stored reference (a pasted Library link into its id). */
  normalizeReference?: (text: string) => string;
  value?: string | undefined;
  file?: File | null;
  invalid?: boolean;
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
    // A reference input sends the media inside the request, so the file is read into a data URI first.
    prepare: fileMode ? undefined : readAsDataUri,
    commit: (picked, reference) => {
      if (fileMode) onFileChange?.(picked);
      else onChange?.(reference);
    },
  });
  const { noun: word } = pick;
  const [previewFailed, setPreviewFailed] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const fileUrl = useObjectUrl(fileMode ? file : null);
  const chosen = fileMode ? Boolean(file) : Boolean(value);
  const preview = fileMode ? fileUrl : value ? previewSource(value) : null;
  const dataUriChosen = !fileMode && Boolean(value?.startsWith("data:"));
  // What the preview draws: the chosen file's own family, else the input's kind.
  const drawn = fileMode && file ? file.type.split("/")[0] : kind;

  const [previousPreviewInput, setPreviousPreviewInput] = useState({
    value,
    file,
  });
  if (
    previousPreviewInput.value !== value ||
    previousPreviewInput.file !== file
  ) {
    setPreviousPreviewInput({ value, file });
    setPreviewFailed(false);
  }

  function clear() {
    pick.clear();
    if (fileMode) onFileChange?.(null);
    else onChange?.(undefined);
  }

  const PlaceholderIcon = kindIcon(kind ?? "file");
  const sourceLabel = !chosen
    ? null
    : (pick.source ??
      (fileMode
        ? (file?.name ?? null)
        : dataUriChosen
          ? null
          : "Added from a link"));
  const emptyHint =
    hint ?? `${word[0].toUpperCase()}${word.slice(1)}s up to ${limitLabel(maxBytes)}.`;

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <div
          className={`bg-muted/40 rounded-media flex size-24 shrink-0 items-center justify-center overflow-hidden border ${
            invalid ? "border-destructive" : "border-input"
          } ${chosen ? "" : "border-dashed"}`}
        >
          {chosen && preview && !previewFailed && (drawn === "video" || drawn === "image") ? (
            drawn === "video" ? (
              <video
                key={previewAttempt}
                src={preview}
                muted
                preload="metadata"
                className="size-full object-cover"
                aria-label={`Chosen ${word}`}
                onError={() => setPreviewFailed(true)}
              />
            ) : (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={previewAttempt}
                src={preview}
                alt={`Chosen ${word}`}
                className="size-full object-cover"
                onError={() => setPreviewFailed(true)}
              />
            )
          ) : (
            <PlaceholderIcon
              aria-hidden="true"
              className="text-muted-foreground size-6"
            />
          )}
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button
              id={id}
              type="button"
              variant="outline"
              size="sm"
              className="max-sm:h-11"
              aria-describedby={describedBy}
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
            {chosen && previewFailed && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="max-sm:h-11"
                aria-label={`Retry preview of ${word} for ${label}`}
                onClick={() => {
                  setPreviewFailed(false);
                  setPreviewAttempt((count) => count + 1);
                }}
              >
                Retry
              </Button>
            )}
            {(chosen || pick.pending) && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="max-sm:h-11"
                aria-label={`Remove ${word} for ${label}`}
                onClick={clear}
              >
                <X aria-hidden="true" />
                Remove
              </Button>
            )}
          </div>
          {pick.pending ? (
            <p role="status" className="text-muted-foreground text-xs">
              Adding {word}…
              {pick.blockedSubmit && " Wait for it to finish, then run again."}
            </p>
          ) : (
            <p className="text-muted-foreground text-xs">
              {sourceLabel ?? emptyHint}
            </p>
          )}
          {chosen && previewFailed && (
            <p role="status" className="text-muted-foreground text-xs">
              Preview unavailable
            </p>
          )}
          {pick.error && (
            <p role="alert" className="text-destructive text-xs">
              {pick.error}
            </p>
          )}
        </div>
      </div>
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
          onPick={(item) => void pick.pickFromLibrary(item)}
        />
      )}
    </div>
  );
}
