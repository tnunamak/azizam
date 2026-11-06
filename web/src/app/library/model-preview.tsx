"use client";

import { useEffect, useRef, useState } from "react";
import { networkFetch } from "@/lib/network-error";
import { MeshViewer } from "@/app/try/mesh-viewer";

/**
 * Opens a stored 3D model in the viewer Try uses. The viewer takes the file itself, so this fetches the signed
 * download link once. The page loads this file only when a model is opened, so three.js stays out of the Library's
 * first bundle.
 */
export default function ModelPreview({
  src,
  title,
  onFailed,
}: {
  src: string;
  title: string;
  onFailed: () => void;
}) {
  const [blob, setBlob] = useState<Blob | null>(null);
  // The caller passes a new function each render; the fetch must not restart for that.
  const failed = useRef(onFailed);
  useEffect(() => {
    failed.current = onFailed;
  }, [onFailed]);
  useEffect(() => {
    let active = true;
    setBlob(null);
    networkFetch(src)
      .then((response) => {
        if (!response.ok) throw new Error("model download failed");
        return response.blob();
      })
      .then((file) => {
        if (active) setBlob(file);
      })
      .catch(() => {
        if (active) failed.current();
      });
    return () => {
      active = false;
    };
  }, [src]);
  if (!blob) return <p role="status">Opening 3D model…</p>;
  // The Library's own Download button saves the file, with the name the gateway gives it.
  return <MeshViewer blob={blob} description={title} padded hideDownload overlayControls />;
}
