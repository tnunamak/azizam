"use client";

import { useRef, useState, type ChangeEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  describeAdapterUploaded,
  uploadAdapter,
  type AdapterUploaded,
} from "@/lib/adapter-upload";
import { cn } from "@/lib/cn";

type UploadStatus =
  | { kind: "idle" }
  | { kind: "pending"; name: string }
  | { kind: "success" | "error"; message: string };

type AdapterUploadControlProps = {
  send: Parameters<typeof uploadAdapter>[1];
  onUploaded: (adapter: AdapterUploaded) => void;
  disabled?: boolean;
};

export function AdapterUploadControl({
  send,
  onUploaded,
  disabled = false,
}: AdapterUploadControlProps) {
  const fileInput = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const [status, setStatus] = useState<UploadStatus>({ kind: "idle" });
  const blocked = disabled || status.kind === "pending";

  async function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    // Allow a retry with the same file after either outcome.
    event.currentTarget.value = "";
    if (!file || disabled || inFlight.current) return;

    inFlight.current = true;
    setStatus({ kind: "pending", name: file.name });
    let result: Awaited<ReturnType<typeof uploadAdapter>>;
    try {
      result = await uploadAdapter(file, send);
    } catch {
      setStatus({ kind: "error", message: "The adapter upload did not finish. Try again." });
      return;
    } finally {
      inFlight.current = false;
    }
    if (!result.ok) {
      setStatus({ kind: "error", message: result.message });
      return;
    }

    setStatus({ kind: "success", message: describeAdapterUploaded(result.adapter) });
    onUploaded(result.adapter);
  }

  return (
    <div className="flex flex-col items-start gap-2">
      <Button
        type="button"
        variant="outline"
        disabled={blocked}
        onClick={() => fileInput.current?.click()}
      >
        Upload your own style adapter
      </Button>
      <input
        ref={fileInput}
        type="file"
        accept=".safetensors"
        data-adapter-file-input
        hidden
        disabled={blocked}
        onChange={chooseFile}
      />
      <div aria-live="polite" aria-atomic="true">
        {status.kind !== "idle" ? (
          <p
            role={status.kind === "error" ? "alert" : undefined}
            className={cn("text-meta", status.kind === "error" ? "text-destructive" : "text-muted-foreground")}
          >
            {status.kind === "pending" ? `Uploading ${status.name}...` : status.message}
          </p>
        ) : null}
      </div>
    </div>
  );
}
