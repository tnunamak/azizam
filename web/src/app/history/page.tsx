"use client";

import { useViewer } from "@/components/shell/session";
import { PageHeader } from "@/components/product/page-header";
import { ErrorState } from "@/components/product/error-state";
import { HistoryList } from "./history-list";

/** What the person's apps sent and what came back. Only the signed-in person's own rows are listed. */
export default function HistoryPage() {
  const viewer = useViewer();
  return (
    <div className="page-read">
      <PageHeader
        title="History"
        description="What your apps sent and what came back. Only you can see it."
      />
      {viewer.serviceAdmin ? (
        <ErrorState
          severity="info"
          heading="Sign in as a person"
          message="This session uses a key, which has no history of its own."
          action={{ label: "Sign in", href: "/sign-in/" }}
        />
      ) : (
        <HistoryList />
      )}
    </div>
  );
}
