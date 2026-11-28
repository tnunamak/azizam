"use client";

import Link from "next/link";
import { useEffect } from "react";
import { ErrorState } from "@/components/product/error-state";
import { PageHeader } from "@/components/product/page-header";
import { Button } from "@/components/ui/button";

/**
 * The crashed state of any page (a render that threw). The app shell stays;
 * the page area says so calmly and offers the two ways out. The member sees no
 * message, stack or digest, and the console gets only a fixed label and the
 * digest, never the error itself.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Only the digest (the id a server log can be searched by) and a fixed label: no message, no stack.
    console.error("Azizam page error", error.digest ?? "no digest");
  }, [error]);
  return (
    <>
      <PageHeader title="This page stopped working" subtitle={null} />
      <ErrorState
        heading="Something went wrong on this page"
        message="Try again. If it keeps happening, go Home and tell the person who runs this gateway."
        action={{ label: "Try again", onClick: reset }}
      />
      <div className="mt-3">
        <Button variant="ghost" size="sm" asChild>
          <Link prefetch={false} href="/">Go home</Link>
        </Button>
      </div>
    </>
  );
}
