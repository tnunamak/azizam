"use client";

import { RefreshCw, ServerOff } from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { EmptyState } from "@/components/product/empty-state";
import { StatusPill } from "@/components/product/status-pill";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useOptionalProviderStatus } from "@/lib/provider-status";

function providerUnavailableReason(provider: {
  enabled: boolean | null;
  reachable: boolean | null;
  keyState: "set" | "missing" | "unknown";
}) {
  if (provider.enabled === false) return "Provider is disabled.";
  if (provider.keyState === "missing") return "Provider key is missing.";
  if (provider.reachable === false) return "Provider is unreachable.";
  return "Provider readiness is not measured yet.";
}

export default function ProvidersPage() {
  const providers = useOptionalProviderStatus();
  return (
    <>
      <PageHeader
        title="Providers"
        answer="Choose which remote providers your gateway can use."
        description="Requests go to a remote provider only when a model or alias names it. We never switch to a paid provider on our own."
      />
      {providers.state === "loading" ? (
        <div
          role="status"
          aria-label="Loading providers"
          aria-busy="true"
          className="space-y-4"
        >
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : providers.state === "hidden" ? (
        <EmptyState
          title="Provider controls are not installed"
          description="This gateway does not expose the provider admin API yet. Local engines and explicit model aliases keep working."
          icon={ServerOff}
        />
      ) : providers.state === "error" ? (
        <div
          role="alert"
          className="border-danger/30 bg-danger/10 text-danger rounded-lg border p-4 text-sm"
        >
          <p>{providers.message}</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={providers.refetch}
          >
            <RefreshCw aria-hidden="true" />
            Retry providers
          </Button>
        </div>
      ) : providers.providers.length === 0 ? (
        <EmptyState
          title="No providers yet"
          description="No remote providers are configured. This page shows status for provider records; review your existing upstream connections under Engines."
          action={
            <Button asChild variant="outline">
              <a href="/ui/engines/">Review connections</a>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {providers.providers.map((provider) => (
            <article
              key={provider.id}
              className="border-border rounded-xl border p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="text-base font-semibold">{provider.name}</h2>
                  <p className="text-muted-foreground mt-1 text-sm">
                    {provider.enabled === true
                      ? "Enabled"
                      : provider.enabled === false
                        ? "Disabled"
                        : "Enablement is not reported"}
                  </p>
                </div>
                <StatusPill
                  status={provider.tone === "ok" ? "ready" : "unavailable"}
                  reason={
                    provider.tone === "ok"
                      ? "Provider is reachable."
                      : providerUnavailableReason(provider)
                  }
                />
              </div>
              <dl className="mt-5 grid gap-3 text-sm sm:grid-cols-2">
                <div>
                  <dt className="text-muted-foreground">Key</dt>
                  <dd className="font-medium">
                    {provider.keyState === "set"
                      ? "Set"
                      : provider.keyState === "missing"
                        ? "Missing"
                        : "Unknown"}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Spend</dt>
                  <dd className="font-medium">{provider.spendText}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
