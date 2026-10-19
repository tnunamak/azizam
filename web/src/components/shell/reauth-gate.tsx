"use client";

import { lazy, Suspense, useState } from "react";
import { usePendingReauth } from "@/lib/reauth";

const ReauthDialog = lazy(() =>
  import("./reauth-dialog").then((module) => ({
    default: module.ReauthDialog,
  })),
);

/** Loads the confirm-it's-you prompt the first time a risky action needs it,
 * so the dialog costs nothing on a page that never asks. */
export function ReauthGate() {
  const info = usePendingReauth();
  const [requested, setRequested] = useState(false);
  if (info && !requested) setRequested(true);
  return requested ? (
    <Suspense fallback={null}>
      <ReauthDialog info={info} />
    </Suspense>
  ) : null;
}
