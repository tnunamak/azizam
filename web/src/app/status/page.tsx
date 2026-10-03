import { Suspense } from "react";
import { StatusClient } from "./status-client";

function StatusFallback() {
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6" aria-busy="true">
      <h1 className="text-3xl font-semibold tracking-tight">Status</h1>
      <div className="bg-card text-muted-foreground rounded-lg border p-4">
        Loading engine status…
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<StatusFallback />}>
      <StatusClient />
    </Suspense>
  );
}
