"use client";

import { useEffect, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
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
  /** The file name the viewer's own download link gives, such as azizam-3d-<timestamp>.glb. Unused when the download is hidden. */
  downloadName?: string;
  /** What the model is, for screen readers. */
  description: string;
  /** The page already has a Download button for the file (the Library): the viewer shows none of its own. */
  hideDownload?: boolean;
  /** Inset the viewer's controls 16 px from the sides, for a viewer that runs edge to edge on a phone. */
  padded?: boolean;
  /**
   * The page frames the model itself (the Library's stage): no border of its own, and Reset view is an icon button
   * over the model, bottom centre, instead of a row of buttons below it.
   */
  overlayControls?: boolean;
};

/** The keys model-viewer turns, tilts or zooms the model with while it has focus. */
const TURN_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown"]);

/**
 * A 3D model viewer: drag or pinch to turn, scroll or pinch to zoom, a Reset view button, and a Download GLB link.
 *
 * model-viewer loads only in the browser, from an effect, so it is never part of the Try page's first load and never
 * runs during the static export. The element is removed on unmount, which releases its WebGL context.
 */
export function MeshViewer({
  blob,
  downloadName,
  description,
  hideDownload = false,
  padded = false,
  overlayControls = false,
}: MeshViewerProps) {
  // The hint over the model goes once the person has turned it.
  const [dragged, setDragged] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const [view, setView] = useState<ViewState>({
    phase: "loading",
    progress: 0,
  });
  const host = useRef<HTMLDivElement>(null);
  const element = useRef<ModelViewerElement | null>(null);

  useEffect(() => {
    const objectUrl = URL.createObjectURL(blob);
    setDragged(false);
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
    import("@google/model-viewer")
      .then(() => {
        if (!active || !host.current) return;
        const viewer = document.createElement(
          "model-viewer",
        ) as ModelViewerElement;
        viewer.setAttribute("src", url);
        viewer.setAttribute("alt", description);
        viewer.setAttribute("camera-controls", "");
        viewer.setAttribute("touch-action", "pan-y");
        // The page shows one hint ("Drag to turn"); model-viewer's own hand icon would be a second.
        viewer.setAttribute("interaction-prompt", "none");
        viewer.setAttribute("shadow-intensity", "1");
        // model-viewer defaults to 300x150 and frames the model for that size, so the element must fill its host.
        viewer.style.width = "100%";
        viewer.style.height = "100%";
        // Spin only for people who have not asked for less motion.
        if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
          viewer.setAttribute("auto-rotate", "");
        }
        viewer.addEventListener("progress", (event: Event) => {
          const total =
            (event as CustomEvent<{ totalProgress?: number }>).detail
              ?.totalProgress ?? 0;
          if (!active || loaded) return;
          arm();
          setView({ phase: "loading", progress: total });
        });
        viewer.addEventListener("load", () => {
          loaded = true;
          clearTimeout(stall);
          // model-viewer's own bar can stick short of full (its final update is throttled), and ours is gone once loaded.
          viewer.style.setProperty("--progress-bar-height", "0px");
          if (active) setView({ phase: "ready" });
        });
        viewer.addEventListener("error", fail);
        host.current.replaceChildren(viewer);
        created = viewer;
        element.current = viewer;
        arm();
      })
      .catch(fail);
    return () => {
      active = false;
      clearTimeout(stall);
      element.current = null;
      // Removing the element disconnects it, which lets model-viewer release the renderer and its WebGL context.
      created?.remove();
    };
  }, [url, description]);

  const percent =
    view.phase === "loading" ? Math.round(view.progress * 100) : 100;
  const resetView = () => {
    element.current?.resetTurntableRotation();
    element.current?.jumpCameraToGoal();
  };
  const status =
    view.phase === "loading"
      ? `Loading the 3D model. ${percent}%`
      : view.phase === "ready"
        ? `3D model loaded. Drag to turn it. Use the ${hideDownload ? "button to reset the view." : "buttons to reset the view or download the file."}`
        : "The 3D model could not be shown in this browser. You can still download the file.";

  return (
    // Full width: the viewer's size must not depend on a line of words beside it (the status line is for screen readers).
    // In the Library the viewer sits in a stage of fixed height: its square stays inside it, with the controls below.
    <div className={cn("w-full space-y-3", padded && "mx-auto max-w-[min(100%,60dvh)]")}>
      <div
        className={cn(
          "bg-muted/40 relative aspect-square w-full overflow-hidden rounded-xl",
          !overlayControls && "border",
        )}
        onPointerDown={() => setDragged(true)}
        onKeyDown={(event) => {
          if (TURN_KEYS.has(event.key)) setDragged(true);
        }}
      >
        <div ref={host} className="absolute inset-0" />
        {view.phase === "ready" && (
          <p
            aria-hidden="true"
            data-slot="drag-hint"
            className={cn(
              "bg-chrome/70 text-chrome-foreground text-meta pointer-events-none whitespace-nowrap absolute top-3 left-1/2 -translate-x-1/2 rounded-full px-3 py-1 transition-opacity duration-500 motion-reduce:transition-none",
              dragged && "opacity-0",
            )}
          >
            Drag to turn
          </p>
        )}
        {overlayControls && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label="Reset view"
            title="Reset view"
            className="bg-background/80 absolute bottom-3 left-1/2 size-11 -translate-x-1/2 rounded-full backdrop-blur-sm"
            disabled={view.phase !== "ready"}
            onClick={resetView}
          >
            <RotateCcw aria-hidden="true" className="size-5" />
          </Button>
        )}
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
      {/* Said aloud, not shown, unless the model cannot be shown: then the sentence is the explanation. */}
      <p
        role="status"
        aria-live="polite"
        className={view.phase === "error" ? "text-muted-foreground text-sm" : "sr-only"}
      >
        {status}
      </p>
      {overlayControls ? null : (
      <div className={cn("flex flex-wrap gap-2", padded && "px-4")}>
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={view.phase !== "ready"}
          onClick={resetView}
        >
          Reset view
        </Button>
        {hideDownload ? null : (
          <a
            href={url ?? undefined}
            download={downloadName}
            aria-disabled={!url || undefined}
            className={cn(
              buttonVariants({ variant: "outline" }),
              "min-h-11",
              !url && "pointer-events-none opacity-50",
            )}
          >
            Download GLB
          </a>
        )}
      </div>
      )}
    </div>
  );
}
