"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useViewerQuery } from "@/components/shell/session";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, PageHeader } from "@/components/product";

export default function LandingPage() {
  const viewer = useViewerQuery();
  const router = useRouter();
  useEffect(() => {
    if (viewer.data?.authenticated) router.replace("/home/");
  }, [viewer.data?.authenticated, router]);
  return (
    <div className="space-y-8">
      <PageHeader title="Home" answer="Here’s how to use your AI." />
      {viewer.isPending || viewer.data?.authenticated ? (
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
          action={
            <Button asChild>
              <Link href="/sign-in/">Sign in</Link>
            </Button>
          }
        />
      )}
    </div>
  );
}
