"use client";

import { useEffect, useRef, useState } from "react";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/** The part of model-viewer's element this wrapper uses. */
type ModelViewerElement = HTMLElement & {
  resetTurntableRotation: () => void;
  jumpCameraToGoal: () => void;
};

/**
 * How long a model may go without a progress or load event before the viewer gives up. model-viewer can fail a
 * fetch without dispatching `error` (a blocked blob: URL, for one), and then nothing else would end the loading state.
 */
const STALL_MS = 30_000;

type ViewState =
  | { phase: "loading"; progress: number }
  | { phase: "ready" }
  | { phase: "error" };

export type MeshViewerProps = {
  /** The GLB the gateway returned. The viewer holds an object URL for it and revokes that URL when it changes or unmounts. */
  blob: Blob;
  /** The file name the download gets, such as azizam-3d-<timestamp>.glb. */
  downloadName: string;
  /** What the model is, for screen readers. */
  description: string;
};

/**
 * A 3D model viewer: drag or pinch to turn, scroll or pinch to zoom, a Reset view button, and a Download GLB link.
 *
 * model-viewer loads only in the browser, from an effect, so it is never part of the Try page's first load and never
 * runs during the static export. The element is removed on unmount, which releases its WebGL context.
 */
export function MeshViewer({ blob, downloadName, description }: MeshViewerProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [view, setView] = useState<ViewState>({ phase: "loading", progress: 0 });
  const host = useRef<HTMLDivElement>(null);
  const element = useRef<ModelViewerElement | null>(null);

  useEffect(() => {
    const objectUrl = URL.createObjectURL(blob);
    setUrl(objectUrl);
    setView({ phase: "loading", progress: 0 });
    return () => {
      URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [blob]);

  useEffect(() => {
    if (!url || !host.current) return;
    let active = true;
    let created: ModelViewerElement | null = null;
    let stall: ReturnType<typeof setTimeout> | undefined;
    // model-viewer can dispatch a final `progress` (100%) after `load`. Once loaded, progress must not reopen the
    // loading state or re-arm the stall timer.
    let loaded = false;
    const fail = () => {
      clearTimeout(stall);
      if (active) setView({ phase: "error" });
    };
    const arm = () => {
      clearTimeout(stall);
      stall = setTimeout(fail, STALL_MS);
    };
    // The import runs here, after mount, so the module is fetched only when a model is shown.
    import("@google/model-viewer").then(() => {
      if (!active || !host.current) return;
      const viewer = document.createElement("model-viewer") as ModelViewerElement;
      viewer.setAttribute("src", url);
      viewer.setAttribute("alt", description);
      viewer.setAttribute("camera-controls", "");
      viewer.setAttribute("touch-action", "pan-y");
      viewer.setAttribute("shadow-intensity", "1");
      // Spin only for people who have not asked for less motion.
      if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
        viewer.setAttribute("auto-rotate", "");
      }
      viewer.addEventListener("progress", (event: Event) => {
        const total = (event as CustomEvent<{ totalProgress?: number }>).detail?.totalProgress ?? 0;
        if (!active || loaded) return;
        arm();
        setView({ phase: "loading", progress: total });
      });
      viewer.addEventListener("load", () => {
        loaded = true;
        clearTimeout(stall);
        if (active) setView({ phase: "ready" });
      });
      viewer.addEventListener("error", fail);
      host.current.replaceChildren(viewer);
      created = viewer;
      element.current = viewer;
      arm();
    }).catch(fail);
    return () => {
      active = false;
      clearTimeout(stall);
      element.current = null;
      // Removing the element disconnects it, which lets model-viewer release the renderer and its WebGL context.
      created?.remove();
    };
  }, [url, description]);

  const percent = view.phase === "loading" ? Math.round(view.progress * 100) : 100;
  const status =
    view.phase === "loading"
      ? `Loading the 3D model. ${percent}%`
      : view.phase === "ready"
        ? "3D model loaded. Drag to turn it. Use the buttons to reset the view or download the file."
        : "The 3D model could not be shown in this browser. You can still download the file.";

  return (
    <div className="space-y-3">
      <div className="bg-muted/40 relative aspect-square w-full overflow-hidden rounded-xl border">
        <div ref={host} className="absolute inset-0" />
        {view.phase === "loading" && (
          <div className="absolute inset-x-4 bottom-4">
            <progress
              className="h-2 w-full"
              max={1}
              value={view.progress}
              aria-label="Loading the 3D model"
            />
          </div>
        )}
        {view.phase === "error" && (
          <div className="text-muted-foreground absolute inset-0 flex items-center justify-center p-6 text-center text-sm">
            The 3D model could not be shown in this browser.
          </div>
        )}
      </div>
      <p role="status" aria-live="polite" className="text-muted-foreground text-sm">
        {status}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={view.phase !== "ready"}
          onClick={() => {
            element.current?.resetTurntableRotation();
            element.current?.jumpCameraToGoal();
          }}
        >
          Reset view
        </Button>
        <a
          href={url ?? undefined}
          download={downloadName}
          aria-disabled={!url || undefined}
          className={cn(buttonVariants({ variant: "outline" }), "min-h-11", !url && "pointer-events-none opacity-50")}
        >
          Download GLB
        </a>
      </div>
    </div>
  );
}
