"use client";
import { useQuery } from "@tanstack/react-query";
import { ErrorState } from "@/components/product/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { getSignIn, signInKeys } from "@/lib/signin-api";
import { headline, providerLabel } from "./copy";
import { hasSavedConnection } from "./form";
import { SignInEditor } from "./sign-in-editor";

/** Settings > Sign-in: ways to sign in, the provider connection, and who gets in. */
export function SignInSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: signInKeys.snapshot,
    queryFn: getSignIn,
    staleTime: 15_000,
    refetchOnWindowFocus: false,
  });
  return (
    <section
      id="sign-in-section"
      data-slot="settings-section"
      aria-labelledby="sign-in"
      className="min-w-0"
    >
      <div className="mb-(--space-heading)">
        <h2 id="sign-in" className="text-heading">
          Sign-in
        </h2>
        {data && (
          <p className="text-muted-foreground text-meta">
            {headline({
              provider: data.methods.provider,
              password: data.methods.password,
              hasConnection: hasSavedConnection(data),
              fallback: data.rules.fallback,
              rules: data.rules.rules,
              label: providerLabel(data.provider.name, data.provider.address),
            })}
          </p>
        )}
      </div>
      {isPending ? (
        <div
          role="status"
          aria-label="Loading sign-in"
          aria-busy="true"
          className="space-y-4"
        >
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-3/4" />
        </div>
      ) : isError ? (
        error instanceof ApiError && error.status === 403 ? (
          <p role="alert" className="text-muted-foreground text-meta">
            Only the owner can change this.
          </p>
        ) : (
          <div className="[&_button]:min-h-11 [&_summary]:min-h-11">
            <ErrorState
              heading="Couldn’t load this"
              error={error}
              message="Sign-in settings could not be loaded."
              onRetry={() => void refetch()}
            />
          </div>
        )
      ) : (
        <SignInEditor snapshot={data} />
      )}
    </section>
  );
}
