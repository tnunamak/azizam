"use client";
import { useEffect, useId, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, ChevronLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/api";
import { isNetworkFailure } from "@/lib/network-error";
import { PRODUCT_NAME } from "@/lib/product";
import type { RememberedPerson } from "@/lib/remembered-people";
import { PersonAvatar, generatedAvatarPalettes } from "@/components/product/person-avatar";
import { authFetch, continueTo, type SignInOptions } from "./auth-api";

export type ProviderError = "provider" | "no_sign_in_time" | "not_fresh" | null;

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
        return "Password sign-in is turned off for everyone but the owner. Reload this page to see how to sign in.";
      case "invalid_password":
        return "Enter a password of up to 1,024 characters.";
    }
  }
  return "Couldn’t sign you in. Check your connection and try again.";
}

// The name a person sees for their provider: the label the owner set, else a plain phrase.
export function providerNameFor(label: string | undefined): string {
  return label && label !== "your identity provider"
    ? label
    : "your sign-in provider";
}

// A refused sign-in: the provider did not ask for a password just now, so no one is signed in. The way out is to sign
// out at the provider. The link appears only when the gateway says the provider publishes a sign-out address.
export function NotFreshNotice({
  provider,
  signOut,
}: {
  provider: string;
  signOut: boolean;
}) {
  return (
    <p role="alert" className="text-body mb-6">
      Azizam couldn’t confirm that {provider} just asked for your password.{" "}
      {signOut ? (
        <>
          <a
            className="text-link underline underline-offset-4"
            href="/login/provider-sign-out"
          >
            Sign out of {provider}
          </a>
          , then try again.
        </>
      ) : (
        <>Sign out of {provider}, then try again.</>
      )}
    </p>
  );
}

/** The provider's own refusals, shown above whichever way to sign in the page offers. */
export function ProviderNotices({
  error,
  signOut,
  providerName,
}: {
  error: ProviderError;
  signOut: boolean;
  providerName: string;
}) {
  if (error === "provider") {
    return (
      <p role="alert" className="text-body mb-6">
        Single sign-on didn’t finish. Try again, or ask the person who
        invited you for help.
      </p>
    );
  }
  if (error === "no_sign_in_time") {
    return (
      <p role="alert" className="text-body mb-6">
        Your sign-in provider doesn’t tell Azizam when you signed in,
        so Azizam can’t tell that you just confirmed. Confirm with your
        Azizam password instead, or use an admin key for this action.
      </p>
    );
  }
  if (error === "not_fresh") {
    return <NotFreshNotice provider={providerName} signOut={signOut} />;
  }
  return null;
}

/** The provider's sign-in address with the name box's answer: remember=1 keeps the name, remember=0 does not. */
function withRemember(url: string, remember: boolean): string {
  return `${url}${url.includes("?") ? "&" : "?"}remember=${remember ? 1 : 0}`;
}

/**
 * The plain sign-in form (S9): email and password, or single sign-on, and the reset form for a reset link.
 * This is the form the page has always shown, with the name box (remember) added under the heading.
 */
export function PlainSignIn({
  data,
  reset,
  returnTo,
  rememberOn,
  fromList,
  onBack,
  providerError,
  providerSignOut,
  providerName,
}: {
  data: SignInOptions | undefined;
  reset: string;
  returnTo: string;
  /** False while the owner remembers nobody: the name box is then not shown, and nothing is kept. */
  rememberOn: boolean;
  /** True when this form was opened from the list, which the back link returns to. */
  fromList: boolean;
  onBack: () => void;
  providerError: ProviderError;
  providerSignOut: boolean;
  providerName: string;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [ownerForm, setOwnerForm] = useState(false);
  const [remember, setRemember] = useState(true);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const rememberId = useId();
  const rememberHelpId = useId();
  useEffect(() => {
    headingRef.current?.focus();
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
            remember: rememberOn ? remember : false,
          }),
    onSuccess: (data) => {
      setPassword("");
      continueTo(data.next);
    },
  });
  useEffect(() => {
    if (login.isError) errorRef.current?.focus();
  }, [login.isError, login.error]);
  const instanceName = data?.instance_name?.trim();
  const provider = data?.oidc_label?.trim();
  const hasProvider = data?.oidc && !reset;
  const providerLabel =
    provider && provider !== "your identity provider"
      ? `Continue with ${provider}`
      : "Continue with single sign-on";
  // While passwords are off for the household, the owner's own password still works.
  const ownerCanUsePassword =
    Boolean(data) && !data?.built_in && data?.owner_password !== false && !reset;
  const showForm = Boolean(data?.built_in) || Boolean(reset) || ownerForm;
  const nameBoxShown = rememberOn && !reset;

  return (
    <>
      {fromList && !reset && (
        <button
          type="button"
          className="text-link text-meta mb-4 flex min-h-11 items-center gap-1 underline underline-offset-4"
          onClick={onBack}
        >
          <ChevronLeft aria-hidden className="size-4" />
          Back to the list
        </button>
      )}
      <h1
        ref={headingRef}
        tabIndex={-1}
        id="sign-in-title"
        className="text-title font-semibold tracking-tight outline-none"
      >
        {reset
          ? "Choose a new password"
          : `Sign in to ${instanceName || PRODUCT_NAME}`}
      </h1>
      {nameBoxShown && (
        <div className="mt-6 space-y-2">
          <div className="flex items-center gap-3">
            {/* A 44 px target around the 16 px box: every control on this page is at least 44 by 44. */}
            <button
              type="button"
              role="checkbox"
              id={rememberId}
              aria-checked={remember}
              aria-describedby={rememberHelpId}
              onClick={() => setRemember((value) => !value)}
              className="focus-visible:ring-ring/50 flex size-11 shrink-0 items-center justify-center rounded-control outline-none focus-visible:ring-[3px]"
            >
              <span
                aria-hidden
                className={cn(
                  "flex size-4 items-center justify-center rounded-sm border",
                  remember ? "bg-primary border-primary text-primary-foreground" : "border-input",
                )}
              >
                {remember && <Check className="size-3.5" />}
              </span>
            </button>
            <Label htmlFor={rememberId} className="text-body font-medium">
              Show my name on this browser next time
            </Label>
          </div>
          <p id={rememberHelpId} className="text-muted-foreground text-meta pl-7">
            Turn this off on a public computer.
          </p>
        </div>
      )}
      <div className="mt-8">
        <ProviderNotices
          error={providerError}
          signOut={providerSignOut}
          providerName={providerName}
        />
        {hasProvider && (
          <Button
            className="h-auto min-h-11 w-full py-2 whitespace-normal!"
            asChild
          >
            <a href={withRemember(data.oidc_url, rememberOn && remember)}>
              {providerLabel}
            </a>
          </Button>
        )}
        {hasProvider && (data.built_in || ownerForm) && (
          <div className="text-muted-foreground text-meta my-6 flex items-center gap-4">
            <span className="flex-1 border-t" />
            <span>or use your email</span>
            <span className="flex-1 border-t" />
          </div>
        )}
        {hasProvider && ownerCanUsePassword && !ownerForm && (
          <p className="text-muted-foreground text-meta mt-6">
            <button
              type="button"
              className="text-link min-h-11 underline underline-offset-4"
              onClick={() => setOwnerForm(true)}
            >
              Owner? Sign in with your password
            </button>
          </p>
        )}
        {showForm && (
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
      </div>
    </>
  );
}

type PasswordFailure =
  | { kind: "wrong" }
  | { kind: "too-many"; minutes: string }
  | { kind: "alert"; message: string };

function passwordFailure(error: unknown): PasswordFailure {
  if (error instanceof ApiError) {
    if (error.code === "invalid_login") return { kind: "wrong" };
    if (error.code === "login_rate_limited") {
      return { kind: "too-many", minutes: error.message.match(/(\d+) minutes?/)?.[1] ?? "15" };
    }
    if (error.code === "account_disabled") {
      return { kind: "alert", message: "Your account is disabled. Ask the person who invited you for help." };
    }
  }
  if (isNetworkFailure(error)) {
    return navigator.onLine === false
      ? { kind: "alert", message: "You’re offline. Try again when you’re connected." }
      : { kind: "alert", message: "Couldn’t reach the gateway. Try again in a moment." };
  }
  return { kind: "alert", message: "Couldn’t sign you in. Check your connection and try again." };
}

/** One remembered person's password screen (S6): their name is the heading, and the password is the only task. */
export function PersonPassword({
  person,
  onBack,
  onGone,
}: {
  person: RememberedPerson;
  onBack: () => void;
  /** The gateway no longer knows this name on this browser (S10). */
  onGone: (tile: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState<PasswordFailure | null>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const helpId = useId();
  const errorId = useId();
  const palette = generatedAvatarPalettes.find((entry) => entry.id === person.avatar.variant)?.id;
  const attempt = useMutation({
    mutationFn: () =>
      authFetch<{ next: string }>("/auth/api/login", {
        tile: person.tile,
        password,
        remember: true,
      }),
    onSuccess: (data) => {
      setPassword("");
      continueTo(data.next);
    },
    onError: (error) => {
      const next = passwordFailure(error);
      setFailure(next);
      if (error instanceof ApiError && error.code === "tile_unknown") onGone(person.tile);
    },
  });
  useEffect(() => {
    passwordRef.current?.focus();
  }, []);
  useEffect(() => {
    // A wrong password keeps the field and selects its text, so the next try replaces it.
    if (!attempt.isError) return;
    passwordRef.current?.focus();
    if (failure?.kind === "wrong") passwordRef.current?.select();
  }, [attempt.isError, attempt.error, failure]);
  const tooMany = failure?.kind === "too-many";
  const wrong = failure?.kind === "wrong";

  return (
    <>
      <div className="flex items-center gap-4">
        <PersonAvatar
          name={person.name}
          selection={palette ? { kind: "generated", variant: palette } : null}
          size="lg"
          decorative
        />
        <h1
          tabIndex={-1}
          id="sign-in-title"
          className="text-title font-semibold tracking-tight outline-none"
        >
          {person.name}
        </h1>
      </div>
      <button
        type="button"
        className="text-link text-meta mt-3 flex min-h-11 items-center gap-1 underline underline-offset-4"
        onClick={onBack}
      >
        <ChevronLeft aria-hidden className="size-4" />
        Not you? Choose someone else
      </button>
      <form
        className="mt-6 space-y-5"
        aria-busy={attempt.isPending}
        onSubmit={(event) => {
          event.preventDefault();
          attempt.mutate();
        }}
      >
        <div className="space-y-2">
          <Label htmlFor={`${helpId}-password`}>Password</Label>
          <Input
            id={`${helpId}-password`}
            ref={passwordRef}
            type="password"
            autoComplete="current-password"
            required
            aria-label={`Password for ${person.name}`}
            aria-invalid={wrong || undefined}
            aria-describedby={wrong ? `${errorId} ${helpId}` : helpId}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={attempt.isPending}
            // On an invalid field the focus ring takes the error colour, so the error stays visible while typing.
            className="min-h-11 aria-invalid:focus-visible:border-destructive aria-invalid:focus-visible:ring-destructive/30"
          />
        </div>
        <p id={helpId} className="text-muted-foreground text-meta">
          This browser is shared. If it offers to save your password, choose Never.
        </p>
        {wrong && (
          <p id={errorId} role="alert" className="text-danger text-body">
            That password isn’t right. Try again.
          </p>
        )}
        {failure?.kind === "too-many" && (
          <p role="alert" className="text-danger text-body">
            Too many tries. Wait {failure.minutes} minutes, then try again.
          </p>
        )}
        {failure?.kind === "alert" && (
          <p role="alert" className="text-danger text-body">
            {failure.message}
          </p>
        )}
        <Button
          type="submit"
          className="h-auto min-h-11 w-full py-2 whitespace-normal!"
          disabled={attempt.isPending || tooMany}
        >
          {attempt.isPending ? "Signing in…" : "Sign in"}
        </Button>
        <p className="text-muted-foreground text-meta">
          Forgot your password? Ask the person who invited you for a reset link.
        </p>
      </form>
    </>
  );
}

/** A remembered person the gateway no longer offers on this browser (S10). Their row is removed from the list. */
export function GoneNotice({
  name,
  onAnotherWay,
  onBack,
}: {
  name: string;
  onAnotherWay: () => void;
  onBack: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  return (
    <>
      <h1
        ref={headingRef}
        tabIndex={-1}
        id="sign-in-title"
        className="text-title font-semibold tracking-tight outline-none"
      >
        {name} can’t sign in from this list
      </h1>
      <p className="text-body mt-4">
        Try another way to sign in, or ask the person who invited you for help.
      </p>
      <div className="mt-6 flex flex-col gap-3 sm:flex-row">
        <Button className="min-h-11" onClick={onAnotherWay}>
          Sign in another way
        </Button>
        <Button className="min-h-11" variant="outline" onClick={onBack}>
          Back to the list
        </Button>
      </div>
    </>
  );
}
