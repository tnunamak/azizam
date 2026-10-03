"use client";

import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/product/error-state";

type SetupState = {
  owner_exists: boolean;
  oidc_enabled: boolean;
  can_recover: boolean;
};

export default function SetupPage() {
  const { data, isPending, isError, isFetching, refetch } =
    useQuery<SetupState>({
      queryKey: ["public", "setup"],
      queryFn: async () => {
        const response = await fetch("/setup/api/state", { cache: "no-store" });
        if (!response.ok) throw new Error("Setup state could not be loaded");
        return response.json();
      },
    });
  const returning = data?.owner_exists;
  return (
    <div className="mx-auto flex w-full max-w-lg flex-col gap-6 py-8 sm:py-12">
      <Card className="min-w-0 gap-6">
        <CardHeader>
          <ShieldCheck
            aria-hidden="true"
            className="text-primary mb-2 size-8"
          />
          <h1 className="text-title font-semibold">
            {returning ? "Sign in to your gateway" : "Set up your gateway"}
          </h1>
          <p className="text-muted-foreground text-sm">
            {!data
              ? "Load your gateway’s sign-in and setup options."
              : returning
                ? "Use your owner account to manage this gateway."
                : "Create the owner account. You’ll connect an engine or provider next."}
          </p>
        </CardHeader>
        <CardContent className="min-w-0 space-y-6">
          {isPending ? (
            <div aria-label="Loading setup" className="space-y-4">
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
          ) : isError ? (
            <ErrorState
              message="Setup options could not be loaded. Check your connection and try again."
              onRetry={() => void refetch()}
              retrying={isFetching}
            />
          ) : (
            <>
              {data.oidc_enabled ? (
                <div className="space-y-4">
                  <p className="text-muted-foreground text-sm">
                    {returning
                      ? "Continue with your configured identity provider."
                      : "The first allowed sign-in becomes this gateway’s owner."}
                  </p>
                  <Button asChild className="w-full">
                    <a href="/login?return_to=/setup">
                      Continue with single sign-on
                    </a>
                  </Button>
                </div>
              ) : (
                <form
                  method="post"
                  action={
                    returning ? "/setup/local-login" : "/setup/local-owner"
                  }
                  className="space-y-4"
                >
                  <div className="space-y-2">
                    <label
                      htmlFor="setup-email"
                      className="text-sm font-medium"
                    >
                      Email or username
                    </label>
                    <Input
                      id="setup-email"
                      name="email"
                      autoComplete="username"
                      spellCheck={false}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <label
                      htmlFor="setup-password"
                      className="text-sm font-medium"
                    >
                      Password
                    </label>
                    <Input
                      id="setup-password"
                      name="password"
                      type="password"
                      autoComplete={
                        returning ? "current-password" : "new-password"
                      }
                      required
                    />
                  </div>
                  {!returning && (
                    <div className="space-y-2">
                      <label
                        htmlFor="setup-password-confirm"
                        className="text-sm font-medium"
                      >
                        Confirm password
                      </label>
                      <Input
                        id="setup-password-confirm"
                        name="password_confirm"
                        type="password"
                        autoComplete="new-password"
                        required
                      />
                    </div>
                  )}
                  <Button type="submit" className="w-full">
                    {returning ? "Sign in" : "Create owner and continue"}
                  </Button>
                </form>
              )}
              <details className="min-w-0 border-t pt-4">
                <summary className="text-muted-foreground min-h-11 cursor-pointer py-3 text-sm font-medium">
                  Recovery and advanced setup
                </summary>
                <div className="min-w-0 space-y-4 pt-2">
                  <p className="text-muted-foreground text-sm">
                    If you cannot sign in, run this command on the gateway host
                    to create a recovery admin key.
                  </p>
                  <pre className="bg-muted max-w-full rounded-md p-3 font-mono text-xs [overflow-wrap:anywhere] whitespace-pre-wrap">
                    ./venv/bin/python proxy.py generate-admin-key owner-cli
                  </pre>
                  {data.can_recover ? (
                    <form
                      method="post"
                      action="/setup/claim"
                      className="space-y-3"
                    >
                      <div className="space-y-2">
                        <label
                          htmlFor="setup-recovery-key"
                          className="text-sm font-medium"
                        >
                          Recovery admin key
                        </label>
                        <Input
                          id="setup-recovery-key"
                          name="admin_key"
                          type="password"
                          autoComplete="off"
                          required
                        />
                      </div>
                      <Button
                        type="submit"
                        variant="outline"
                        className="w-full"
                      >
                        Continue with recovery key
                      </Button>
                    </form>
                  ) : (
                    <p className="text-muted-foreground text-sm">
                      Browser recovery is not enabled on this gateway. Configure
                      browser admin sessions on the host before using a recovery
                      key.
                    </p>
                  )}
                </div>
              </details>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
