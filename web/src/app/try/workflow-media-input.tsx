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
import { SNIFF_BYTES, sniffKinds } from "@/lib/media-sniff";
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

function previewSource(value: string): string | null {
  // A Library asset id is not an address: its download route answers with a
  // link, not the bytes, so a pasted id has no preview.
  return /^data:(?:image|video)\//i.test(value) || /^https?:\/\//i.test(value)
    ? value
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
  // The one read or download that may still change the form. Anything that
  // supersedes it (another choice, Remove, a reopened Library, unmount) aborts
  // its requests, ends its pending state and leaves a late answer unheard.
  const active = useRef<{ controller: AbortController } | null>(null);
  useEffect(
    () => () => {
      active.current?.controller.abort();
      active.current = null;
    },
    [],
  );
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

  function supersede() {
    active.current?.controller.abort();
    const next = { controller: new AbortController() };
    active.current = next;
    setPending(false);
    return next;
  }

  function startPending() {
    setPending(true);
    setBlockedSubmit(false);
  }

  async function choose(file: File | undefined) {
    if (!file) return;
    setFileError(null);
    // A rejected file must not cancel a pending valid choice, so only a file
    // that passes validation supersedes it.
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
    const operation = supersede();
    if (fileMode) {
      onFileChange?.(file);
      setSource(file.name);
      return;
    }
    startPending();
    try {
      const value = await readAsDataUri(file);
      if (active.current !== operation) return;
      onChange?.(value);
      setSource(file.name);
      setPending(false);
    } catch {
      if (active.current !== operation) return;
      setPending(false);
      setFileError(`Could not read that ${noun}. Try another file.`);
    }
  }

  async function pickFromLibrary(pick: LibraryPick) {
    const operation = supersede();
    setFileError(null);
    setLibraryOpen(false);
    // The request carries the bytes, as it does for "Choose a file". A bare
    // asset id would only work for the app that made the item, and the
    // Library lists everything the person made with any of their apps.
    startPending();
    try {
      const picked = await libraryFile(
        pick,
        kind,
        noun,
        operation.controller.signal,
      );
      if (active.current !== operation) return;
      const reference = fileMode ? null : await readAsDataUri(picked);
      if (active.current !== operation) return;
      if (fileMode) onFileChange?.(picked);
      else onChange?.(reference ?? undefined);
      setSource("From your Library");
      setPending(false);
    } catch (error) {
      if (active.current !== operation) return;
      setPending(false);
      setFileError(
        error instanceof TooBigError
          ? error.message
          : `Could not add that ${noun} from your Library. Try another, or choose a file.`,
      );
    }
  }

  function clear() {
    supersede();
    setSource(null);
    setFileError(null);
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
              onClick={() => {
                supersede();
                setLibraryOpen(true);
              }}
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
                supersede();
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
        onPick={(pick) => void pickFromLibrary(pick)}
      />
    </div>
  );
}

type LibraryAsset = {
  asset_id: string;
  kind?: string;
  media_type?: string | null;
  size_bytes?: number | null;
  prompt?: string | null;
  available?: boolean;
  // Signed links the list carries for showing the item; they are not for sending.
  thumbnail_url?: string | null;
  download_url?: string | null;
};

type LibraryPick = Pick<LibraryAsset, "asset_id" | "media_type" | "size_bytes">;

type LibraryPage = { assets?: LibraryAsset[]; next_cursor?: string | null };

const PAGE_SIZE = 40;

/** The item is over the size limit; the message is what the person reads. */
class TooBigError extends Error {
  constructor(noun: string, bytes?: number | null) {
    const size = bytes ? `is ${megabytes(bytes)}` : `is over ${MAX_MEDIA_LABEL}`;
    super(`That ${noun} ${size}. Choose one under ${MAX_MEDIA_LABEL}.`);
  }
}

/**
 * Reads a body without holding more than the media limit: it stops as soon as
 * the bytes passed the limit (a missing or wrong Content-Length cannot get
 * round that) or the leading bytes are not the kind of media the input takes.
 */
async function readMedia(
  response: Response,
  kind: MediaKind,
  noun: string,
  knownBytes: number | null,
  signal: AbortSignal,
): Promise<Uint8Array[]> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Library download is empty");
  const chunks: Uint8Array[] = [];
  let total = 0;
  let sniffed = false;
  const check = () => {
    const head = new Uint8Array(SNIFF_BYTES);
    let filled = 0;
    for (const chunk of chunks) {
      const part = chunk.subarray(0, SNIFF_BYTES - filled);
      head.set(part, filled);
      filled += part.length;
      if (filled >= SNIFF_BYTES) break;
    }
    if (!sniffKinds(head.subarray(0, filled)).includes(kind)) {
      throw new Error("Library download is not media");
    }
    sniffed = true;
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error("Library download was replaced");
      if (done) break;
      total += value.length;
      if (total > MAX_MEDIA_BYTES) throw new TooBigError(noun, knownBytes);
      chunks.push(value);
      if (!sniffed && total >= SNIFF_BYTES) check();
    }
    if (!sniffed) check();
  } catch (error) {
    void reader.cancel().catch(() => {});
    throw error;
  }
  return chunks;
}

/**
 * The bytes of one Library item as a file. The download route answers with a
 * short-lived signed link, so this asks for a fresh one and then reads the
 * file from it, as the Library page does. The list's size and the response's
 * Content-Length refuse a too-big item before any of it is read.
 */
async function libraryFile(
  pick: LibraryPick,
  kind: MediaKind,
  noun: string,
  signal: AbortSignal,
): Promise<File> {
  if (pick.size_bytes && pick.size_bytes > MAX_MEDIA_BYTES) {
    throw new TooBigError(noun, pick.size_bytes);
  }
  const link = await fetch(
    `/me/api/media/${encodeURIComponent(pick.asset_id)}/download?scope=me`,
    { credentials: "same-origin", signal },
  );
  if (!link.ok) throw new Error("Library download failed");
  const { download_url: url } = (await link.json()) as {
    download_url?: string | null;
  };
  if (!url) throw new Error("Library download has no link");
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error("Library download failed");
  // An error page must not become a picture: the type is the media's own, or
  // the gateway's declared type when the file comes back as plain bytes. The
  // bytes themselves are checked as they arrive.
  const header = response.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  const declared = pick.media_type?.toLowerCase();
  const type = header?.startsWith(`${kind}/`)
    ? header
    : (!header || header === "application/octet-stream") &&
        declared?.startsWith(`${kind}/`)
      ? declared
      : null;
  if (!type) throw new Error("Library download is not media");
  const length = Number(response.headers.get("content-length"));
  if (length > MAX_MEDIA_BYTES) {
    void response.body?.cancel().catch(() => {});
    throw new TooBigError(noun, length);
  }
  const chunks = await readMedia(response, kind, noun, length || null, signal);
  const extension = SUBTYPE.exec(type)?.[1] ?? "bin";
  return new File(chunks as BlobPart[], `library-${noun}.${extension}`, {
    type,
  });
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
  onPick: (pick: LibraryPick) => void;
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
    } else {
      // The list's signed links are not kept once the dialog is closed.
      setState("idle");
      setAssets([]);
      setNextCursor(null);
      setUnshown(new Set());
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
    return () => {
      controller.abort();
      // A "Show more" answer still in flight belongs to a closed list.
      generation.current += 1;
    };
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
              // The list's own signed links show the item; the download route
              // answers with a link, not an image.
              const link =
                (kind === "image" ? asset.thumbnail_url : null) ??
                asset.download_url;
              const src = link && !unshown.has(asset.asset_id) ? link : null;
              return (
                <li key={asset.asset_id}>
                  <button
                    type="button"
                    title={title}
                    aria-label={title}
                    onClick={() => onPick(asset)}
                    className="rounded-media focus-visible:ring-ring/50 bg-muted/40 flex aspect-square w-full items-center justify-center overflow-hidden border focus-visible:ring-[3px] focus-visible:outline-none"
                  >
                    {kind === "audio" || !src ? (
                      <span className="text-muted-foreground flex flex-col items-center gap-1 p-1 text-xs">
                        {kind === "audio" ? (
                          <Music aria-hidden="true" className="size-6" />
                        ) : (
                          <ImageIcon aria-hidden="true" className="size-6" />
                        )}
                        {kind !== "audio" && "Preview unavailable"}
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
