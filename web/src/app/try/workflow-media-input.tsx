"use client";

import { useEffect, useRef, useState } from "react";
import { ImageIcon, Library, Music, Upload, Video, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/product";
import {
  MEDIA_NOUN,
  withArticle,
  type MediaKind,
} from "@/lib/workflow-media-input";
import {
  MAX_MEDIA_BYTES,
  MAX_MEDIA_LABEL,
  assetIdFromPasted,
} from "./workflow-input-meta";
const ASSET_ID = /^(?:asset:)?((?:img|vid|aud)_[0-9a-f]{64})$/;

function previewSource(value: string): string | null {
  if (/^data:(?:image|video)\//i.test(value) || /^https?:\/\//i.test(value)) return value;
  const asset = ASSET_ID.exec(value);
  return asset
    ? `/me/api/media/${encodeURIComponent(asset[1])}/download?scope=me`
    : null;
}

function readAsDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("Read failed"));
    reader.readAsDataURL(file);
  });
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

const SUBTYPE = /^[a-z]+\/([a-z0-9]+)/i;

function megabytes(bytes: number) {
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/**
 * Picks a media input. It has two modes, one control either way:
 *
 * - **reference** (`value`/`onChange`): the media travels inside the request
 *   body as a data URI, a link or a Library asset id. The raw reference sits
 *   under Advanced.
 * - **file** (`file`/`onFileChange`): the media travels as a multipart file
 *   part. A Library picture is downloaded into a file; there is no link box.
 *
 * A text box that asks for "value" is not a way to choose a picture, so the
 * main path is a file or the Library.
 */
export function MediaInput({
  id,
  label,
  kind,
  value,
  file,
  invalid,
  describedBy,
  onChange,
  onFileChange,
}: {
  id: string;
  label: string;
  kind: MediaKind;
  value?: string | undefined;
  file?: File | null;
  invalid?: boolean;
  describedBy?: string;
  onChange?: (value: string | undefined) => void;
  onFileChange?: (file: File | null) => void;
}) {
  const noun = MEDIA_NOUN[kind];
  const fileMode = onFileChange !== undefined;
  const fileInput = useRef<HTMLInputElement>(null);
  const selection = useRef(0);
  useEffect(() => () => {
    selection.current += 1;
  }, []);
  const [source, setSource] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  // A read or download is under way: the choice is not in the form yet.
  const [pending, setPending] = useState(false);
  const [blockedSubmit, setBlockedSubmit] = useState(false);
  const fileUrl = useObjectUrl(fileMode ? file : null);
  const chosen = fileMode ? Boolean(file) : Boolean(value);
  const preview = fileMode ? fileUrl : value ? previewSource(value) : null;
  const dataUriChosen = !fileMode && Boolean(value?.startsWith("data:"));

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

  // Run must not go out while the choice is still arriving: it would send the
  // form without it. The page owns Run, so the pending input stops the form.
  useEffect(() => {
    const form = pending ? fileInput.current?.closest("form") : null;
    if (!form) return;
    const stop = (event: Event) => {
      event.preventDefault();
      event.stopPropagation();
      setBlockedSubmit(true);
    };
    form.addEventListener("submit", stop, true);
    return () => form.removeEventListener("submit", stop, true);
  }, [pending]);

  function startPending() {
    setPending(true);
    setBlockedSubmit(false);
  }

  async function choose(file: File | undefined) {
    if (!file) return;
    setFileError(null);
    // A rejected file must not cancel a pending valid choice, so the selection
    // generation advances only after the file passes validation.
    if (!file.type.startsWith(`${kind}/`)) {
      setFileError(
        `That file is not ${withArticle(noun)}. Choose ${withArticle(noun)} file.`,
      );
      return;
    }
    if (file.size > MAX_MEDIA_BYTES) {
      setFileError(
        `That ${noun} is ${megabytes(file.size)}. Choose one under ${MAX_MEDIA_LABEL}.`,
      );
      return;
    }
    const operation = ++selection.current;
    if (fileMode) {
      onFileChange?.(file);
      setSource(file.name);
      return;
    }
    startPending();
    try {
      const value = await readAsDataUri(file);
      if (selection.current !== operation) return;
      onChange?.(value);
      setSource(file.name);
      setPending(false);
    } catch {
      if (selection.current !== operation) return;
      setPending(false);
      setFileError(`Could not read that ${noun}. Try another file.`);
    }
  }

  async function pickFromLibrary(assetId: string) {
    const operation = ++selection.current;
    setFileError(null);
    setLibraryOpen(false);
    if (!fileMode) {
      setSource("From your Library");
      onChange?.(assetId);
      return;
    }
    // A file part needs the bytes: fetch the Library picture into a file.
    startPending();
    try {
      const response = await fetch(
        `/me/api/media/${encodeURIComponent(assetId)}/download?scope=me`,
        { credentials: "same-origin" },
      );
      if (!response.ok) throw new Error("Library download failed");
      const blob = await response.blob();
      if (selection.current !== operation) return;
      const type = blob.type || `${kind}/png`;
      const extension = SUBTYPE.exec(type)?.[1] ?? "png";
      onFileChange?.(new File([blob], `library-${noun}.${extension}`, { type }));
      setSource("From your Library");
      setPending(false);
    } catch {
      if (selection.current !== operation) return;
      setPending(false);
      setFileError(
        `Could not add that ${noun} from your Library. Try another, or choose a file.`,
      );
    }
  }

  function clear() {
    selection.current += 1;
    setSource(null);
    setFileError(null);
    setPending(false);
    if (fileMode) onFileChange?.(null);
    else onChange?.(undefined);
  }

  const PlaceholderIcon =
    kind === "video" ? Video : kind === "audio" ? Music : ImageIcon;
  const sourceLabel = !chosen
    ? null
    : (source ??
      (fileMode
        ? (file?.name ?? null)
        : dataUriChosen
          ? null
          : "Added from a link"));

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <div
          className={`bg-muted/40 rounded-media flex size-24 shrink-0 items-center justify-center overflow-hidden border ${
            invalid ? "border-destructive" : "border-input"
          } ${chosen ? "" : "border-dashed"}`}
        >
          {chosen && preview && !previewFailed ? (
            kind === "video" ? (
              <video
                key={previewAttempt}
                src={preview}
                muted
                preload="metadata"
                className="size-full object-cover"
                aria-label={`Chosen ${noun}`}
                onError={() => setPreviewFailed(true)}
              />
            ) : (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={previewAttempt}
                src={preview}
                alt={`Chosen ${noun}`}
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
              onClick={() => fileInput.current?.click()}
            >
              <Upload aria-hidden="true" />
              Choose a file
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="max-sm:h-11"
              onClick={() => setLibraryOpen(true)}
            >
              <Library aria-hidden="true" />
              Choose from Library
            </Button>
            {chosen && previewFailed && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="max-sm:h-11"
                aria-label={`Retry preview of ${noun} for ${label}`}
                onClick={() => {
                  setPreviewFailed(false);
                  setPreviewAttempt((count) => count + 1);
                }}
              >
                Retry
              </Button>
            )}
            {(chosen || pending) && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="max-sm:h-11"
                aria-label={`Remove ${noun} for ${label}`}
                onClick={clear}
              >
                <X aria-hidden="true" />
                Remove
              </Button>
            )}
          </div>
          {pending ? (
            <p role="status" className="text-muted-foreground text-xs">
              Adding {noun}…
              {blockedSubmit && " Wait for it to finish, then run again."}
            </p>
          ) : (
            <p className="text-muted-foreground text-xs">
              {sourceLabel ??
                `${noun[0].toUpperCase()}${noun.slice(1)}s up to ${MAX_MEDIA_LABEL}.`}
            </p>
          )}
          {chosen && previewFailed && (
            <p role="status" className="text-muted-foreground text-xs">
              Preview unavailable
            </p>
          )}
          {fileError && (
            <p role="alert" className="text-destructive text-xs">
              {fileError}
            </p>
          )}
        </div>
      </div>
      <input
        ref={fileInput}
        type="file"
        accept={`${kind}/*`}
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        data-media-file-input=""
        onChange={(event) => {
          const input = event.currentTarget;
          void choose(input.files?.[0]).finally(() => {
            input.value = "";
          });
        }}
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
                const next = assetIdFromPasted(event.currentTarget.value);
                selection.current += 1;
                setSource(null);
                onChange?.(next || undefined);
              }}
            />
          </label>
        </details>
      )}
      <LibraryPicker
        open={libraryOpen}
        onOpenChange={setLibraryOpen}
        kind={kind}
        onPick={(assetId) => void pickFromLibrary(assetId)}
      />
    </div>
  );
}

type LibraryAsset = {
  asset_id: string;
  kind?: string;
  prompt?: string | null;
  available?: boolean;
};

type LibraryPage = { assets?: LibraryAsset[]; next_cursor?: string | null };

const PAGE_SIZE = 40;

function downloadUrl(assetId: string) {
  return `/me/api/media/${encodeURIComponent(assetId)}/download?scope=me`;
}

function LibraryPicker({
  open,
  onOpenChange,
  kind,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: MediaKind;
  onPick: (assetId: string) => void;
}) {
  const noun = MEDIA_NOUN[kind];
  const [state, setState] = useState<"idle" | "loading" | "error" | "ready">(
    "idle",
  );
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [more, setMore] = useState<"idle" | "loading" | "error">("idle");
  const [unshown, setUnshown] = useState<Set<string>>(new Set());
  const [attempt, setAttempt] = useState(0);
  const [previousOpen, setPreviousOpen] = useState(open);
  // Each reload or reopen is a new list; a "Show more" answer for an older
  // one must not land in it.
  const generation = useRef(0);
  if (open !== previousOpen) {
    setPreviousOpen(open);
    if (open) {
      setState("loading");
      setMore("idle");
    }
  }

  function pageUrl(cursor?: string | null) {
    const query = new URLSearchParams({
      kind,
      limit: String(PAGE_SIZE),
      scope: "me",
    });
    if (cursor) query.set("cursor", cursor);
    return `/me/api/media?${query}`;
  }

  function usable(page: LibraryPage) {
    return (page.assets ?? []).filter(
      (asset) =>
        asset.asset_id &&
        asset.available !== false &&
        (asset.kind ?? kind) === kind,
    );
  }

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const mine = ++generation.current;
    fetch(pageUrl(), { credentials: "same-origin", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Library unavailable");
        const page = (await response.json()) as LibraryPage;
        if (generation.current !== mine) return;
        setAssets(usable(page));
        setNextCursor(page.next_cursor ?? null);
        setUnshown(new Set());
        setState("ready");
      })
      .catch(() => {
        if (!controller.signal.aborted) setState("error");
      });
    return () => controller.abort();
    // pageUrl and usable only read `kind`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, attempt, kind]);

  async function showMore() {
    if (!nextCursor) return;
    const mine = generation.current;
    setMore("loading");
    try {
      const response = await fetch(pageUrl(nextCursor), {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Library unavailable");
      const page = (await response.json()) as LibraryPage;
      if (generation.current !== mine) return;
      setAssets((current) => {
        const seen = new Set(current.map((asset) => asset.asset_id));
        return [...current, ...usable(page).filter((a) => !seen.has(a.asset_id))];
      });
      setNextCursor(page.next_cursor ?? null);
      setMore("idle");
    } catch {
      if (generation.current === mine) setMore("error");
    }
  }

  function markUnshown(assetId: string) {
    setUnshown((current) => new Set(current).add(assetId));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Choose {withArticle(noun)} from your Library</DialogTitle>
          <DialogDescription>
            {noun[0].toUpperCase()}
            {noun.slice(1)}s you made, newest first.
          </DialogDescription>
        </DialogHeader>
        {state === "loading" && (
          <div
            aria-busy="true"
            className="grid grid-cols-3 gap-2 sm:grid-cols-4"
          >
            {Array.from({ length: 8 }, (_, index) => (
              <Skeleton key={index} className="aspect-square w-full" />
            ))}
          </div>
        )}
        {state === "error" && (
          <ErrorState
            heading="Could not open your Library"
            message={`Your ${noun}s could not be loaded.`}
            action={{
              label: "Try loading it again",
              onClick: () => setAttempt((count) => count + 1),
            }}
          />
        )}
        {state === "ready" && assets.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No {noun}s in your Library yet. Make one, or choose a file.
          </p>
        )}
        {state === "ready" && assets.length > 0 && (
          <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {assets.map((asset) => {
              const title =
                asset.prompt?.trim().slice(0, 80) ||
                `${noun[0].toUpperCase()}${noun.slice(1)} from your Library`;
              const src = downloadUrl(asset.asset_id);
              const broken = unshown.has(asset.asset_id);
              return (
                <li key={asset.asset_id}>
                  <button
                    type="button"
                    title={title}
                    aria-label={title}
                    onClick={() => onPick(asset.asset_id)}
                    className="rounded-media focus-visible:ring-ring/50 bg-muted/40 flex aspect-square w-full items-center justify-center overflow-hidden border focus-visible:ring-[3px] focus-visible:outline-none"
                  >
                    {broken || kind === "audio" ? (
                      <span className="text-muted-foreground flex flex-col items-center gap-1 p-1 text-xs">
                        {kind === "audio" ? (
                          <Music aria-hidden="true" className="size-6" />
                        ) : (
                          <ImageIcon aria-hidden="true" className="size-6" />
                        )}
                        {broken ? "Preview unavailable" : null}
                        {kind === "audio" && (
                          <span className="line-clamp-3">{title}</span>
                        )}
                      </span>
                    ) : kind === "video" ? (
                      <video
                        src={src}
                        muted
                        preload="metadata"
                        className="size-full object-cover"
                        onError={() => markUnshown(asset.asset_id)}
                      />
                    ) : (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={src}
                        alt=""
                        loading="lazy"
                        className="size-full object-cover"
                        onError={() => markUnshown(asset.asset_id)}
                      />
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        {state === "ready" && more === "error" && (
          <p role="alert" className="text-destructive text-xs">
            Could not load more {noun}s. Try again.
          </p>
        )}
        {state === "ready" && nextCursor && (
          <Button
            type="button"
            variant="outline"
            className="min-h-11"
            disabled={more === "loading"}
            onClick={() => void showMore()}
          >
            {more === "loading" ? "Loading…" : "Show more"}
          </Button>
        )}
      </DialogContent>
    </Dialog>
  );
}
