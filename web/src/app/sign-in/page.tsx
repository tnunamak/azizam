"use client";
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Fish, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState, EmptyState } from "@/components/product";
import { PRODUCT_NAME } from "@/lib/product";
import { authFetch, continueTo, type SignInOptions } from "./auth-api";

export default function SignInPage() {
  const [returnTo, setReturnTo] = useState("/ui/home/");
  const {
    data: optionsData,
    isPending: optionsIsPending,
    isError: optionsIsError,
    refetch: optionsRefetch,
  } = useQuery({
    queryKey: ["auth", "options", returnTo],
    queryFn: () =>
      authFetch<SignInOptions>(
        `/auth/api/options?return_to=${encodeURIComponent(returnTo)}`,
      ),
  });
  const [reset, setReset] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  useEffect(() => {
    setReturnTo(
      new URLSearchParams(window.location.search).get("return_to") ||
        "/ui/home/",
    );
    setReset(
      new URLSearchParams(window.location.hash.slice(1)).get("reset") || "",
    );
  }, []);
  const login = useMutation({
    mutationFn: () =>
      reset
        ? authFetch<{ next: string }>("/auth/api/password-reset", {
            token: reset,
            password,
          })
        : authFetch<{ next: string }>("/auth/api/login", {
            email,
            password,
            return_to: returnTo,
          }),
    onSuccess: (data) => {
      setPassword("");
      setReset("");
      continueTo(data.next);
    },
  });
  return (
    <section className="mx-auto flex w-full max-w-md flex-col py-12 sm:py-20">
      <div className="bg-card rounded-xl border p-6 sm:p-8">
        <Fish className="text-primary mb-6 size-8" aria-hidden="true" />
        <h1 className="text-2xl font-semibold tracking-tight">
          {reset ? "Choose a new password" : "Sign in"}
        </h1>
        <p className="text-muted-foreground mt-2 mb-8 text-sm">
          {reset
            ? "Use at least 12 characters to protect your account."
            : `Connect to ${optionsData?.instance_name || PRODUCT_NAME}, your own AI provider.`}
        </p>
        {optionsIsPending ? (
          <div
            role="status"
            aria-label="Loading sign-in options"
            className="space-y-4"
          >
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-11 w-full" />
          </div>
        ) : optionsIsError ? (
          <ErrorState
            message="Sign-in options could not be loaded. Try again."
            onRetry={() => void optionsRefetch()}
          />
        ) : (
          <>
            {(optionsData.built_in || reset) && (
              <form
                className="space-y-5"
                onSubmit={(e) => {
                  e.preventDefault();
                  login.mutate();
                }}
              >
                {!reset && (
                  <div className="space-y-2">
                    <Label htmlFor="email">Email</Label>
                    <Input
                      id="email"
                      type="email"
                      autoComplete="username"
                      required
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      disabled={login.isPending}
                    />
                  </div>
                )}
                <div className="space-y-2">
                  <Label htmlFor="password">
                    {reset ? "New password" : "Password"}
                  </Label>
                  <Input
                    id="password"
                    type="password"
                    autoComplete={reset ? "new-password" : "current-password"}
                    minLength={reset ? 12 : undefined}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    disabled={login.isPending}
                  />
                </div>
                {login.isError && (
                  <p role="alert" className="text-destructive text-sm">
                    {login.error.message}
                  </p>
                )}
                <Button className="w-full" disabled={login.isPending}>
                  {login.isPending
                    ? "Please wait…"
                    : reset
                      ? "Save password"
                      : "Sign in"}
                  <ArrowRight />
                </Button>
                {!reset && (
                  <p className="text-muted-foreground text-xs leading-5">
                    Forgot your password? Ask the gateway owner for a reset
                    link.
                  </p>
                )}
              </form>
            )}
            {optionsData.oidc && !reset && (
              <div className={optionsData.built_in ? "mt-6 border-t pt-6" : ""}>
                <Button variant="outline" className="w-full" asChild>
                  <a href={optionsData.oidc_url}>
                    Continue with{" "}
                    {optionsData.oidc_label || "your identity provider"}
                  </a>
                </Button>
              </div>
            )}
            {!optionsData.built_in && !optionsData.oidc && !reset && (
              <EmptyState
                title="Sign-in is not configured"
                description="Ask the gateway owner to enable accounts or an identity provider in Settings."
              />
            )}
          </>
        )}
      </div>
      <p className="text-muted-foreground mt-6 text-center text-xs">
        One account for your apps, keys and creations.
      </p>
    </section>
  );
}
