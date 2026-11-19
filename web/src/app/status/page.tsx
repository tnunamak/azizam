import { Suspense } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusView } from "./status-view";

function StatusFallback() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">Status</h1>
      <p role="status" aria-label="Loading status" className="sr-only">Loading status</p>
      <Skeleton aria-hidden="true" className="h-10 w-2/3" />
      <Skeleton aria-hidden="true" className="rounded-panel h-64 w-full" />
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
