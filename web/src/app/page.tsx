"use client";

import { useViewerQuery } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, PageHeader } from "@/components/product";
import HomePage from "./_home/home-page";

// "/" is a public page (gateway/ui_pages.json): signed-out visitors see how to
// start, signed-in people get Home. No redirect between the two.
export default function RootPage() {
  const viewer = useViewerQuery();
  if (viewer.data?.authenticated) return <HomePage />;
  return (
    <div className="space-y-8">
      <PageHeader title="Home" answer="Here’s how to use your AI." />
      {viewer.isPending ? (
        <div role="status" aria-label="Loading Home" aria-busy="true">
          <Skeleton className="h-48 w-full" />
        </div>
      ) : viewer.isError ? (
        <ErrorState
          message="Your account could not be loaded. Try again."
          onRetry={() => void viewer.refetch()}
        />
      ) : (
        <EmptyState
          title="Your own AI, in the apps you use"
          description="Sign in to get your gateway address, create a key, and connect your first app."
          // Rendered only after the session check, so window exists.
          action={
            <Button asChild>
              <a
                href={`/sign-in/?return_to=${encodeURIComponent(window.location.pathname + window.location.search + window.location.hash)}`}
              >
                Sign in
              </a>
            </Button>
          }
        />
      )}
    </div>
  );
}
