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

function megabytes(bytes: number) {
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/**
 * Picks a media input that travels inside the request body (a data URI, a
 * link or a Library asset id). A text box that asks for "value" is not a way
 * to choose a picture, so the main path is a file or the Library; the raw
 * reference sits under Advanced.
 */
export function MediaInput({
  id,
  label,
  kind,
  value,
  invalid,
  describedBy,
  onChange,
}: {
  id: string;
  label: string;
  kind: MediaKind;
  value: string | undefined;
  invalid?: boolean;
  describedBy?: string;
  onChange: (value: string | undefined) => void;
}) {
  const noun = MEDIA_NOUN[kind];
  const fileInput = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);
  const preview = value ? previewSource(value) : null;
  const dataUriChosen = Boolean(value?.startsWith("data:"));

  useEffect(() => setPreviewFailed(false), [value]);

  async function choose(file: File | undefined) {
    if (!file) return;
    setFileError(null);
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
    try {
      onChange(await readAsDataUri(file));
      setSource(file.name);
    } catch {
      setFileError(`Could not read that ${noun}. Try another file.`);
    }
  }

  function clear() {
    setSource(null);
    setFileError(null);
    onChange(undefined);
  }

  const sourceLabel = source ?? (value && !dataUriChosen ? "Added from a link" : null);

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <div
          className={`bg-muted/40 rounded-media flex size-24 shrink-0 items-center justify-center overflow-hidden border ${
            invalid ? "border-destructive" : "border-input"
          } ${value ? "" : "border-dashed"}`}
        >
          {value && preview && !previewFailed ? (
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
            {value && (
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
              setSource(null);
              onChange(next || undefined);
            }}
          />
        </label>
      </details>
      <LibraryPicker
        open={libraryOpen}
        onOpenChange={setLibraryOpen}
        onPick={(assetId) => {
          setSource("From your Library");
          setFileError(null);
          onChange(assetId);
          setLibraryOpen(false);
        }}
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
    "idle",
  );
  const [assets, setAssets] = useState<LibraryAsset[]>([]);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setState("loading");
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
                asset.prompt?.trim().slice(0, 80) || "Picture from your Library";
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
