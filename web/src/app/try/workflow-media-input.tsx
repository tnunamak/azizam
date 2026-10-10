"use client";

import { useEffect, useRef, useState } from "react";
import { ImageIcon, Library, Upload, X } from "lucide-react";
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
  if (/^data:image\//i.test(value) || /^https?:\/\//i.test(value)) return value;
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
  const [preview, setPreview] = useState<{
    file: File;
    url: string;
  } | null>(null);
  const [previousFile, setPreviousFile] = useState(file);
  if (previousFile !== file) {
    setPreviousFile(file);
    setPreview(null);
  }
  useEffect(() => {
    if (!file || typeof URL.createObjectURL !== "function") return;
    const next = URL.createObjectURL(file);
    setPreview({ file, url: next });
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return preview && preview.file === file ? preview.url : null;
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
  const fileUrl = useObjectUrl(fileMode ? file : null);
  const chosen = fileMode ? Boolean(file) : Boolean(value);
  const preview = fileMode ? fileUrl : value ? previewSource(value) : null;
  const dataUriChosen = !fileMode && Boolean(value?.startsWith("data:"));

  const [previousInput, setPreviousInput] = useState({ value, file });
  if (previousInput.value !== value || previousInput.file !== file) {
    setPreviousInput({ value, file });
    setPreviewFailed(false);
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
    try {
      const value = await readAsDataUri(file);
      if (selection.current !== operation) return;
      onChange?.(value);
      setSource(file.name);
    } catch {
      if (selection.current !== operation) return;
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
    } catch {
      if (selection.current !== operation) return;
      setFileError(
        `Could not add that ${noun} from your Library. Try another, or choose a file.`,
      );
    }
  }

  function clear() {
    selection.current += 1;
    setSource(null);
    setFileError(null);
    if (fileMode) onFileChange?.(null);
    else onChange?.(undefined);
  }

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
                src={preview}
                muted
                preload="metadata"
                className="size-full object-cover"
                aria-label={`Chosen ${noun}`}
              />
            ) : (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={preview}
                alt={`Chosen ${noun}`}
                className="size-full object-cover"
                onError={() => setPreviewFailed(true)}
              />
            )
          ) : (
            <ImageIcon
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
            {kind === "image" && (
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
            )}
            {chosen && (
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
          <p className="text-muted-foreground text-xs">
            {sourceLabel ??
              `${noun[0].toUpperCase()}${noun.slice(1)}s up to ${MAX_MEDIA_LABEL}.`}
          </p>
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

function LibraryPicker({
  open,
  onOpenChange,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (assetId: string) => void;
}) {
  const [state, setState] = useState<"idle" | "loading" | "error" | "ready">(
    open ? "loading" : "idle",
  );
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [previousRequest, setPreviousRequest] = useState({ open, attempt });
  if (
    previousRequest.open !== open ||
    previousRequest.attempt !== attempt
  ) {
    setPreviousRequest({ open, attempt });
    if (open) setState("loading");
  }

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    fetch("/me/api/media?kind=image&limit=40&scope=me", {
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Library unavailable");
        const payload = (await response.json()) as { assets?: LibraryAsset[] };
        setAssets(
          (payload.assets ?? []).filter(
            (asset) =>
              asset.asset_id &&
              asset.available !== false &&
              (asset.kind ?? "image") === "image",
          ),
        );
        setState("ready");
      })
      .catch(() => {
        if (!controller.signal.aborted) setState("error");
      });
    return () => controller.abort();
  }, [open, attempt]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Choose a picture from your Library</DialogTitle>
          <DialogDescription>
            Pictures you made, newest first.
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
            message="Your pictures could not be loaded."
            action={{
              label: "Try loading it again",
              onClick: () => setAttempt((count) => count + 1),
            }}
          />
        )}
        {state === "ready" && assets.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No pictures in your Library yet. Make one, or choose a file.
          </p>
        )}
        {state === "ready" && assets.length > 0 && (
          <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {assets.map((asset) => {
              const title =
                asset.prompt?.trim().slice(0, 80) ||
                "Picture from your Library";
              return (
                <li key={asset.asset_id}>
                  <button
                    type="button"
                    title={title}
                    aria-label={title}
                    onClick={() => onPick(asset.asset_id)}
                    className="rounded-media focus-visible:ring-ring/50 block aspect-square w-full overflow-hidden border focus-visible:ring-[3px] focus-visible:outline-none"
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={`/me/api/media/${encodeURIComponent(asset.asset_id)}/download?scope=me`}
                      alt=""
                      loading="lazy"
                      className="size-full object-cover"
                    />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
