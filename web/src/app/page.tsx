"use client";

import { useViewerQuery } from "@/components/shell/session";
import { ErrorState } from "@/components/product";
import HomePage from "./_home/home-page";
import SignInPage from "./sign-in/page";

// "/" is Home for a signed-in person and the sign-in page for anyone else (gateway/ui_pages.json). No redirect
// between them. Nothing shows while the session is checked, so the sign-in page never flashes at a signed-in person,
// and a failed check is not read as "signed out".
export default function RootPage() {
  const viewer = useViewerQuery();
  if (viewer.data?.authenticated) return <HomePage />;
  if (viewer.isPending) return null;
  if (viewer.isError) {
    // Same copy as the shell's session failure on every other page.
    return (
      <ErrorState
        heading="Couldn’t load your account"
        message="We couldn't tell why your account didn't load. Try again."
        onRetry={() => void viewer.refetch()}
        retrying={viewer.isFetching}
      />
    );
  }
  return <SignInPage />;
}
