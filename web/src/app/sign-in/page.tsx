"use client";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api";
import { PRODUCT_NAME } from "@/lib/product";
import { authFetch, continueTo, type SignInOptions } from "./auth-api";

function signInError(error: Error): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "invalid_login":
        return "Email or password is incorrect. Check both and try again.";
      case "account_disabled":
        return "Your account is disabled. Ask the person who invited you for help.";
      case "link_unavailable":
        return "This reset link has expired or has already been used. Ask for a new link.";
      case "login_rate_limited":
        return error.message;
      case "builtin_disabled":
        return "Password sign-in is turned off. Reload this page to see how to sign in.";
      case "invalid_password":
        return "Use a password between 12 and 1,024 characters.";
    }
  }
  return "Couldn’t sign you in. Check your connection and try again.";
}

export default function SignInPage() {
  const [returnTo, setReturnTo] = useState<string | null>(null);
  const [reset, setReset] = useState("");
  const [providerError, setProviderError] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const errorRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setReturnTo(params.get("return_to") || "/ui/home/");
    setProviderError(params.get("error") === "provider");
    setReset(
      new URLSearchParams(window.location.hash.slice(1)).get("reset") || "",
    );
  }, []);
  const {
    data: optionsData,
    isPending: optionsIsPending,
    isError: optionsIsError,
    refetch: optionsRefetch,
  } = useQuery({
    queryKey: ["auth", "options", returnTo],
    enabled: returnTo !== null,
    queryFn: () =>
      authFetch<SignInOptions>(
        `/auth/api/options?return_to=${encodeURIComponent(returnTo!)}`,
      ),
  });
  const {
    data: setupData,
    isPending: setupIsPending,
    isError: setupIsError,
    refetch: setupRefetch,
  } = useQuery({
    queryKey: ["auth", "setup"],
    queryFn: () => authFetch<{ owner_exists: boolean }>("/setup/api/state"),
  });
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
  useEffect(() => {
    if (login.isError) errorRef.current?.focus();
  }, [login.isError, login.error]);
  const loading = optionsIsPending || setupIsPending;
  const unavailable = optionsIsError || setupIsError;
  const firstRun = setupData?.owner_exists === false && !reset;
  const data = optionsData;
  const provider = data?.oidc_label?.trim();
  const providerLabel =
    provider && provider !== "your identity provider"
      ? `Continue with ${provider}`
      : "Continue with single sign-on";
  const instanceName = data?.instance_name?.trim();
  const hasProvider = data?.oidc && !reset;

  return (
    <section
      aria-labelledby="sign-in-title"
      className="mx-auto w-full max-w-md py-6 sm:py-12"
    >
      <div className="bg-card rounded-panel border p-6 sm:p-8">
        <h1
          id="sign-in-title"
          className="text-title font-semibold tracking-tight"
        >
          {reset
            ? "Choose a new password"
            : firstRun
              ? "Set up Azizam"
              : `Sign in to ${instanceName || PRODUCT_NAME}`}
        </h1>
        {reset && (
          <p
            id="password-help"
            className="text-muted-foreground text-body mt-2"
          >
            Use at least 12 characters.
          </p>
        )}
        <div className="mt-8">
          {loading ? (
            <p role="status" className="text-muted-foreground text-body">
              Getting sign-in ready…
            </p>
          ) : unavailable ? (
            <div className="space-y-4">
              <p role="alert" className="text-body">
                Couldn’t load sign-in. Check your connection and try again.
              </p>
              <Button
                className="min-h-11"
                variant="outline"
                onClick={() => {
                  void optionsRefetch();
                  void setupRefetch();
                }}
              >
                Try again
              </Button>
            </div>
          ) : firstRun ? (
            <div className="space-y-6">
              <p className="text-muted-foreground text-body">
                Create the first owner account to get started. If you were
                invited, ask your host to finish setup.
              </p>
              <Button
                className="h-auto min-h-11 w-full py-2 whitespace-normal!"
                asChild
              >
                <a href="/ui/setup/">Set up your gateway</a>
              </Button>
            </div>
          ) : (
            <>
              {providerError && (
                <p role="alert" className="text-body mb-6">
                  Single sign-on didn’t finish. Try again, or ask the person who
                  invited you for help.
                </p>
              )}
              {hasProvider && (
                <Button
                  className="h-auto min-h-11 w-full py-2 whitespace-normal!"
                  asChild
                >
                  <a href={data.oidc_url}>{providerLabel}</a>
                </Button>
              )}
              {hasProvider && data.built_in && (
                <div className="text-muted-foreground text-meta my-6 flex items-center gap-4">
                  <span className="flex-1 border-t" />
                  <span>or use your email</span>
                  <span className="flex-1 border-t" />
                </div>
              )}
              {(data?.built_in || reset) && (
                <form
                  className="space-y-5"
                  aria-busy={login.isPending}
                  onSubmit={(event) => {
                    event.preventDefault();
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
                        onChange={(event) => setEmail(event.target.value)}
                        disabled={login.isPending}
                        className="min-h-11"
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
                      aria-describedby={reset ? "password-help" : undefined}
                      minLength={reset ? 12 : undefined}
                      required
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      disabled={login.isPending}
                      className="min-h-11"
                    />
                  </div>
                  {login.isError && (
                    <p
                      ref={errorRef}
                      tabIndex={-1}
                      role="alert"
                      className="text-danger text-body rounded-control"
                    >
                      {signInError(login.error)}
                    </p>
                  )}
                  <Button
                    type="submit"
                    variant={hasProvider ? "outline" : "default"}
                    className="h-auto min-h-11 w-full py-2 whitespace-normal!"
                    disabled={login.isPending}
                  >
                    {login.isPending
                      ? reset
                        ? "Saving password…"
                        : "Signing in…"
                      : reset
                        ? "Save password"
                        : "Sign in"}
                  </Button>
                  {!reset && (
                    <p className="text-muted-foreground text-meta">
                      Forgot your password? Ask the person who invited you for a
                      reset link.
                    </p>
                  )}
                </form>
              )}
              {!data?.built_in && !data?.oidc && !reset && (
                <div className="space-y-2">
                  <h2 className="text-body font-medium">
                    Sign-in is not configured
                  </h2>
                  <p className="text-muted-foreground text-body">
                    Ask your host to turn on a sign-in method.
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
