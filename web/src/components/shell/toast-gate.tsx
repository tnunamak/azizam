"use client";

import { lazy, Suspense, useEffect } from "react";
import { markToasterReady, useToastRequested } from "@/lib/toast";

const Toaster = lazy(() =>
  import("@/components/ui/sonner").then((module) => ({
    default: module.Toaster,
  })),
);

function MountedToaster() {
  useEffect(markToasterReady, []);
  return <Toaster />;
}

export function ToastGate() {
  const requested = useToastRequested();

  return requested ? (
    <Suspense fallback={null}>
      <MountedToaster />
    </Suspense>
  ) : null;
}
