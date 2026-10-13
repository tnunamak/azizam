"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { ErrorState } from "@/components/product/error-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { accountApi } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import { PRODUCT_NAME } from "@/lib/product";

/** Leave for the app's address. Kept apart so tests can watch navigation. */
export const navigate = {
  to(url: string) {
    window.location.assign(url);
  },
};

/**
 * The one consent screen for an MCP client that asked to sign in
 * (gateway/mcp_oauth.py). The gateway already checked the client and its
 * return address; this page shows who is asking, as whom, and what it may do,
 * and sends the answer. Nothing is granted unless the person chooses Allow.
 */
export default function AuthorizePage() {
  const [requestId, setRequestId] = useState<string | null>(null);
  useEffect(() => {
    setRequestId(
      new URLSearchParams(window.location.search).get("request") ?? "",
    );
  }, []);

  const details = useQuery({
    queryKey: ["mcp", "consent", requestId],
    queryFn: () => accountApi.mcpConsentRequest(requestId!),
    enabled: Boolean(requestId),
    retry: false,
  });
  const decide = useMutation({
    mutationFn: (decision: "allow" | "deny") =>
      accountApi.decideMcpConsent(requestId!, decision),
    onSuccess: ({ redirect }) => navigate.to(redirect),
  });

  if (requestId === "")
    return (
      <div className="page-read">
        <PageHeader title="Connect an app" />
        <ErrorState
          heading="Nothing to approve"
          message="Start connecting from the app you want to use."
          severity="info"
        />
      </div>
    );

  if (!requestId || details.isPending)
    return (
      <div className="page-read" aria-busy="true">
        <PageHeader title="Connect an app" />
        <span className="sr-only" role="status">
          Loading the request…
        </span>
        <Skeleton className="h-64 w-full" />
      </div>
    );

  if (details.isError) {
    const expired =
      details.error instanceof ApiError && details.error.status === 410;
    return (
      <div className="page-read">
        <PageHeader title="Connect an app" />
        <ErrorState
          heading={
            expired ? "This request has ended" : "Couldn't load the request"
          }
          message={
            expired
              ? "It expired, was already answered, or was opened in another browser. Start connecting again from the app."
              : "Try again, or start connecting again from the app."
          }
          severity={expired ? "info" : "critical"}
          onRetry={expired ? undefined : () => void details.refetch()}
        />
      </div>
    );
  }

  const { client, person, plan, access, lasts, can_connect } = details.data;
  const failure =
    decide.error instanceof ApiError ? decide.error.message : null;
  return (
    <div className="page-read">
      <PageHeader
        title={`Connect ${client.name} to ${PRODUCT_NAME}?`}
        subtitle={`You're signed in as ${person.email ? `${person.name} (${person.email})` : person.name}.`}
      />
      <div className="page-stack">
        <section
          aria-label="What you are approving"
          className="bg-card text-card-foreground rounded-panel flex flex-col gap-4 border p-4"
        >
          <div>
            <h2 className="text-heading">It will be able to</h2>
            <ul className="mt-2 list-disc pl-5">
              {access.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <p className="text-muted-foreground text-meta mt-2">
              As you, within your plan: <strong>{plan.name}</strong>. It
              can&apos;t change your plan, keys, people or settings. {lasts}.
            </p>
          </div>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">App</dt>
            <dd className="min-w-0 break-words">
              {client.verified_domain ? (
                <>
                  Published by <strong>{client.verified_domain}</strong>
                </>
              ) : (
                <>
                  Registered itself.{" "}
                  <strong>
                    {PRODUCT_NAME} can&apos;t confirm who made it.
                  </strong>
                </>
              )}
            </dd>
            <dt className="text-muted-foreground">Returns to</dt>
            <dd className="min-w-0 break-words">{client.redirect_host}</dd>
          </dl>
          <p className="text-meta">
            Only allow this if you just started connecting {client.name}{" "}
            yourself.
          </p>
        </section>
        {!can_connect ? (
          <ErrorState
            heading="Your account can't use AI here yet"
            message="Ask the owner for access, then connect again."
            severity="attention"
          />
        ) : null}
        {failure ? (
          <p role="alert" className="text-destructive text-sm">
            {failure}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {/* Deny by default: Cancel has focus, so Enter does not approve. */}
          <Button
            variant="outline"
            autoFocus
            disabled={decide.isPending}
            onClick={() => decide.mutate("deny")}
          >
            Cancel
          </Button>
          <Button
            disabled={!can_connect || decide.isPending}
            onClick={() => decide.mutate("allow")}
          >
            {decide.isPending ? (
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            ) : null}
            Allow
          </Button>
        </div>
      </div>
    </div>
  );
}
