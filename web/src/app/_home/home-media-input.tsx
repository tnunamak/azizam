"use client";

import { useEffect, useId, useRef, useState, type DragEvent } from "react";
import { AudioLines, FileIcon, ImageIcon, Paperclip, Upload, Video, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { MediaInput } from "./home-model";
import { fitsMediaInput, mediaPhrase } from "./home-model";

/** The icon for the modality's IANA top-level type; any other modality gets the plain file icon. */
function iconFor(modality: string) {
  if (modality === "image") return ImageIcon;
  if (modality === "audio") return AudioLines;
  if (modality === "video") return Video;
  return FileIcon;
}

function sizeWords(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/** A preview address for a chosen picture, revoked when the file changes. */
function usePreview(file: File | null, input: MediaInput): string | null {
  const [preview, setPreview] = useState<{ file: File; url: string } | null>(null);
  const previewable = input.modality === "image" && file !== null && typeof URL.createObjectURL === "function";
  useEffect(() => {
    if (!previewable || !file) return;
    const url = URL.createObjectURL(file);
    setPreview({ file, url });
    return () => URL.revokeObjectURL(url);
  }, [file, previewable]);
  return preview && preview.file === file ? preview.url : null;
}

/**
 * The composer's file input for one declared input modality (the way Midjourney, Runway and Meshy ask for a source
 * picture): a drop target that is also a button. Drop, choose or paste a file; on a phone the file input's accept
 * filter lets the system offer the camera. A chosen file shows as a thumbnail (a picture) or a name, with a way to
 * take it out. Nothing here names a kind of work: the modality and its accept filter are the data.
 */
export function HomeMediaInput({
  input,
  file,
  onFile,
  disabled = false,
  children,
}: {
  input: MediaInput;
  file: File | null;
  onFile: (file: File | null) => void;
  disabled?: boolean;
  /** An extra control under the prompt line, such as a hand-off to record in Try. */
  children?: React.ReactNode;
}) {
  const inputId = useId();
  const picker = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const preview = usePreview(file, input);
  const Icon = iconFor(input.modality);

  function take(candidate: File | undefined) {
    if (!candidate) return;
    if (!fitsMediaInput(candidate, input)) {
      setRefused(`That is not a ${input.word}. Choose a ${input.word}.`);
      return;
    }
    setRefused(null);
    onFile(candidate);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (disabled) return;
    take(Array.from(event.dataTransfer.files).find((candidate) => fitsMediaInput(candidate, input)) ?? event.dataTransfer.files[0]);
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
        "mx-(--composer-pad) mt-(--composer-pad) mb-2 rounded-[calc(var(--radius-composer)-0.5rem)] border border-dashed px-4 py-4 transition-colors duration-(--motion-fast)",
        dragging ? "border-ring bg-accent" : "border-composer-edge-hover bg-muted/40",
      )}
    >
      <input
        ref={picker}
        id={inputId}
        type="file"
        accept={input.accept}
        disabled={disabled}
        className="sr-only"
        aria-label={`Choose ${mediaPhrase(input)}`}
        onChange={(event) => {
          take(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
      {file ? (
        <div className="flex items-center gap-3">
          {preview ? (
            // A local object address of the person's own picture: next/image has nothing to optimise.
            // eslint-disable-next-line @next/next/no-img-element
            <img src={preview} alt={`Your ${input.word}: ${file.name}`} className="size-16 shrink-0 rounded-lg object-cover" />
          ) : (
            <span className="bg-muted text-muted-foreground flex size-16 shrink-0 items-center justify-center rounded-lg">
              <Icon aria-hidden="true" className="size-6" />
            </span>
          )}
          <p className="min-w-0 flex-1">
            <span className="block truncate font-medium">{file.name}</span>
            <span className="text-muted-foreground text-meta block">{sizeWords(file.size)}</span>
          </p>
          <button
            type="button"
            onClick={() => onFile(null)}
            aria-label={`Remove ${file.name}`}
            className="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring inline-flex size-9 shrink-0 items-center justify-center rounded-full focus-visible:ring-2 focus-visible:outline-none max-sm:size-11"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-3 text-center sm:py-4">
          <Icon aria-hidden="true" className="text-muted-foreground size-6" />
          <p className="text-muted-foreground max-sm:hidden">
            Drop {mediaPhrase(input)} here
          </p>
          <button
            type="button"
            disabled={disabled}
            onClick={() => picker.current?.click()}
            className="border-border text-foreground text-meta hover:bg-muted focus-visible:ring-ring inline-flex h-9 items-center gap-1.5 rounded-full border px-4 font-medium transition-colors duration-(--motion-fast) focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none max-sm:min-h-11"
          >
            <Upload aria-hidden="true" className="size-4" />
            Choose {mediaPhrase(input)}
          </button>
          {children}
        </div>
      )}
      {refused ? (
        <p role="alert" className="text-destructive text-meta mt-2">
          {refused}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The picked file as a chip above the text box, for a capability whose main input is words and whose file is the
 * second thing it takes (an edit): a thumbnail or icon, the name, and a way to take it out.
 */
export function PickedFileChip({
  input,
  file,
  onFile,
}: {
  input: MediaInput;
  file: File;
  onFile: (file: File | null) => void;
}) {
  const preview = usePreview(file, input);
  const Icon = iconFor(input.modality);
  return (
    <div data-slot="home-picked-file" data-modality={input.modality} className="px-(--composer-pad) pt-3">
      <span className="bg-muted text-meta inline-flex max-w-full items-center gap-2 rounded-full py-1 pr-1 pl-1">
        {preview ? (
          // A local object address of the person's own picture: next/image has nothing to optimise.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={preview} alt={`Your ${input.word}: ${file.name}`} className="size-8 shrink-0 rounded-full object-cover" />
        ) : (
          <span className="text-muted-foreground flex size-8 shrink-0 items-center justify-center">
            <Icon aria-hidden="true" className="size-4" />
          </span>
        )}
        <span className="min-w-0 truncate font-medium">{file.name}</span>
        <button
          type="button"
          onClick={() => onFile(null)}
          aria-label={`Remove ${file.name}`}
          className="text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-ring inline-flex size-8 shrink-0 items-center justify-center rounded-full focus-visible:ring-2 focus-visible:outline-none max-sm:size-11"
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </span>
    </div>
  );
}

/**
 * The attach button in the toolbar, for the same kind of capability. It opens the file dialog (on a phone the accept
 * filter lets the system offer the camera). A file of the wrong kind is refused with a sentence.
 */
export function AttachFileButton({
  input,
  file,
  onFile,
  disabled = false,
}: {
  input: MediaInput;
  file: File | null;
  onFile: (file: File | null) => void;
  disabled?: boolean;
}) {
  const picker = useRef<HTMLInputElement>(null);
  const [refused, setRefused] = useState<string | null>(null);
  return (
    <>
      <input
        ref={picker}
        type="file"
        accept={input.accept}
        disabled={disabled}
        className="sr-only"
        tabIndex={-1}
        aria-label={`Choose ${mediaPhrase(input)}`}
        onChange={(event) => {
          const candidate = event.target.files?.[0];
          event.target.value = "";
          if (!candidate) return;
          if (!fitsMediaInput(candidate, input)) {
            setRefused(`That is not a ${input.word}. Choose a ${input.word}.`);
            return;
          }
          setRefused(null);
          onFile(candidate);
        }}
      />
      <button
        type="button"
        disabled={disabled}
        onClick={() => picker.current?.click()}
        aria-label={file ? `Change ${input.word}` : `Attach ${mediaPhrase(input)}`}
        title={file ? `Change ${input.word}` : `Attach ${mediaPhrase(input)}`}
        className="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring inline-flex size-8 shrink-0 items-center justify-center rounded-full transition-colors duration-(--motion-fast) focus-visible:ring-2 focus-visible:outline-none disabled:opacity-50 max-sm:size-11"
      >
        <Paperclip aria-hidden="true" className="size-4" />
      </button>
      {refused ? (
        <p role="alert" className="text-destructive text-meta basis-full">
          {refused}
        </p>
      ) : null}
    </>
  );
}
