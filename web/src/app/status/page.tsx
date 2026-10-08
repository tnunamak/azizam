import { Suspense } from "react";
import { StatusView } from "./status-view";

function StatusFallback() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true">
      <h1 className="text-2xl font-semibold tracking-tight">Status</h1>
      <div className="bg-card text-muted-foreground rounded-lg border p-4">
        Loading status…
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<StatusFallback />}>
      <StatusView />
    </Suspense>
  );
}
