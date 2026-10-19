"use client";

import Link from "next/link";
import { useEffect } from "react";
import { ErrorState } from "@/components/product/error-state";
import { PageHeader } from "@/components/product/page-header";
import { Button } from "@/components/ui/button";

/**
 * The crashed state of any page (a render that threw). The app shell stays;
 * the page area says so calmly and offers the two ways out. The error goes to
 * the console for whoever runs the gateway and is never drawn: no message, no
 * stack, no digest.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
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
