"use client";
import { useEffect, useId, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, ChevronLeft, CircleAlert, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/api";
import { isNetworkFailure } from "@/lib/network-error";
import { PRODUCT_NAME } from "@/lib/product";
import type { RememberedPerson } from "@/lib/remembered-people";
import { PersonAvatar, generatedAvatarPalettes } from "@/components/product/person-avatar";
import { useReportSignInBusy } from "@/components/shell/sign-in-frame";
import { authFetch, continueTo, type SignInOptions } from "./auth-api";
import { notFreshFor } from "./sign-in-status";

export type ProviderError = "provider" | "no_sign_in_time" | "not_fresh" | "tile_unknown" | null;

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

/**
 * A refused sign-in (S8 and S8b, DESIGN.md section 6): the provider did not ask for a password just now, so no one is
 * signed in, and the way out is at the provider. The sign-out link appears only when the gateway says the provider
 * publishes a sign-out address. Back leaves the screen for the sign-in page.
 */
export function NotFreshScreen({
  provider,
  signOut,
  signInAgain,
  onBack,
}: {
  provider: string;
  signOut: boolean;
  /** The single sign-on start, offered when there is no sign-out address (S8b). */
  signInAgain: string;
  onBack: () => void;
}) {
  const copy = notFreshFor(provider, signOut);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  return (
    <>
      <CircleAlert aria-hidden className="text-warning mb-3 size-5" />
      <h1
        ref={headingRef}
        tabIndex={-1}
        id="sign-in-title"
        className="text-3xl font-semibold tracking-[-0.02em] outline-none"
      >
        {copy.title}
      </h1>
      <div className="mt-2 space-y-2">
        {copy.body.map((line) => (
          <p key={line} className="text-body text-muted-foreground">
            {line}
          </p>
        ))}
      </div>
      <div className="mt-8 flex flex-col gap-3 sm:flex-row">
        {signOut ? (
          <Button size="xl" className="w-full whitespace-normal! sm:w-auto" asChild>
            <a href="/login/provider-sign-out">Sign out of {provider}</a>
          </Button>
        ) : (
          <Button size="xl" className="w-full whitespace-normal! sm:w-auto" asChild>
            <a href={signInAgain}>Sign in again</a>
          </Button>
        )}
        <Button className="min-h-11 w-full sm:w-auto" variant="ghost" onClick={onBack}>
          Back
        </Button>
      </div>
    </>
  );
}

/** The provider's own refusals, shown above whichever way to sign in the page offers. */
export function ProviderNotices({ error }: { error: ProviderError }) {
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
  // A single sign-on name this browser no longer holds (DESIGN.md section 10, S10). The address carries no name, so the
  // heading says "That name" where S10 names the person; the sentence after it is S10's, word for word.
  if (error === "tile_unknown") {
    return (
      <p role="alert" className="text-body mb-6">
        That name can’t sign in from this list. Try another way to sign in, or
        ask the person who invited you for help.
      </p>
    );
  }
  return null;
}

/** The provider's sign-in address with the name box's answer: remember=1 keeps the name, remember=0 does not. */
export function withRemember(url: string, remember: boolean): string {
  return `${url}${url.includes("?") ? "&" : "?"}remember=${remember ? 1 : 0}`;
}

/**
 * The plain sign-in form (S9): single sign-on, email and password, or the reset form for a reset link. The name box
 * (remember) sits under the single sign-on button when there is one, and after the form's Sign in when there is only a
 * form (DESIGN.md 4.9).
 */
export function PlainSignIn({
  data,
  reset,
  returnTo,
  rememberOn,
  fromList,
  onBack,
  providerError,
  openOwnerForm = false,
  onOpening,
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
  /** True to open the owner's password form at once (the list's owner link), instead of behind the link. */
  openOwnerForm?: boolean;
  /** Called when the provider's button starts its trip, so the page can drop any line from the last action. */
  onOpening?: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [ownerForm, setOwnerForm] = useState(openOwnerForm);
  const [remember, setRemember] = useState(true);
  // True once the provider's button has been pressed: the page is leaving, so its controls wait.
  const [opening, setOpening] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const errorId = useId();
  const rememberId = useId();
  const rememberHelpId = useId();
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  // The owner's password form opens in place, and its first field takes the focus (S3).
  useEffect(() => {
    if (ownerForm) emailRef.current?.focus();
  }, [ownerForm]);
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
  // A refused sign-in is said once, in the alert. The focus goes to the password field, where the next try starts; a
  // refused password keeps its text selected, so the next try replaces it. Both fields name the alert (aria-describedby).
  const refused = login.isError && login.error instanceof ApiError && login.error.code === "invalid_login";
  const busy = login.isPending || opening;
  useReportSignInBusy(busy);
  useEffect(() => {
    if (!login.isError) return;
    passwordRef.current?.focus();
    if (refused) passwordRef.current?.select();
  }, [login.isError, login.error, refused]);
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
  // The primary beside single sign-on is an outline until either field has text, then the ink pill: the form is in use.
  const filled = email !== "" || password !== "";
  // The name box is one row on the column's left edge (DESIGN.md 4.9). It sits directly under the single sign-on button
  // whenever there is one, above the divider, so it never sits below the main action. With no provider it sits after the
  // form's Sign in. It keeps the name for this browser's list only, and the session is not changed.
  // The 44 px target keeps every control at least 44 by 44; its vertical margins are -14 px, so it takes 16 px of
  // height, and its 14 px overhang puts the box on the column's edge. The label tucks 4 px into the target's clear
  // space, which leaves 10 px between the box and the label. The helper is indented by the box plus those 10 px.
  const nameBox = nameBoxShown ? (
    <div>
      <div className="flex items-center">
        <button
          type="button"
          role="checkbox"
          id={rememberId}
          aria-checked={remember}
          aria-describedby={rememberHelpId}
          disabled={busy}
          onClick={() => setRemember((value) => !value)}
          className="group -my-3.5 -ml-3.5 flex size-11 shrink-0 items-center justify-center rounded-control outline-none disabled:opacity-50"
        >
          <span
            aria-hidden
            className={cn(
              "flex size-4 items-center justify-center rounded-sm border group-focus-visible:outline-2 group-focus-visible:outline-offset-2 group-focus-visible:outline-solid group-focus-visible:outline-ring",
              remember ? "bg-primary border-primary text-primary-foreground" : "border-input",
            )}
          >
            {remember && <Check className="size-3.5" />}
          </span>
        </button>
        <Label htmlFor={rememberId} className="-ml-1 text-body font-medium">
          Remember my name on this browser
        </Label>
      </div>
      {/* 26 px: the 16 px box and the 10 px gap, so the helper starts where the label does. */}
      <p id={rememberHelpId} className="text-muted-foreground text-meta text-pretty mt-0.5 pl-[26px]">
        Show my name here next time. Turn off on a shared computer.
      </p>
    </div>
  ) : null;

  return (
    <>
      {fromList && !reset && (
        <button
          type="button"
          className="text-link text-meta mb-4 -mx-1.5 flex min-h-11 items-center gap-1 rounded-md px-1.5 underline underline-offset-4 disabled:opacity-50"
          disabled={busy}
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
        className="text-3xl font-semibold tracking-[-0.02em] break-words outline-none"
      >
        {reset
          ? "Choose a new password"
          : `Sign in to ${instanceName || PRODUCT_NAME}`}
      </h1>
      <div className="mt-8">
        <ProviderNotices error={providerError} />
        {hasProvider && (
          <>
            <Button
              size="xl"
              variant="make"
              className="w-full whitespace-normal!"
              aria-disabled={busy || undefined}
              tabIndex={busy ? -1 : undefined}
              asChild
            >
              <a
                href={withRemember(data.oidc_url, rememberOn && remember)}
                onClick={(event) => {
                  if (busy) {
                    event.preventDefault();
                    return;
                  }
                  // A modified click opens the provider elsewhere, and this page stays as it is.
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  setOpening(true);
                  onOpening?.();
                }}
              >
                {opening && <Loader2 aria-hidden className="motion-safe:animate-spin motion-reduce:hidden" />}
                {opening ? `Opening ${providerNameFor(provider)}…` : providerLabel}
              </a>
            </Button>
            {opening && (
              <p role="status" className="sr-only">
                Opening {providerNameFor(provider)}
              </p>
            )}
          </>
        )}
        {hasProvider && nameBox && <div className="mt-4">{nameBox}</div>}
        {hasProvider && showForm && (
          <div className="text-muted-foreground text-meta my-6 flex items-center gap-4">
            <span className="flex-1 border-t" />
            <span>or use your email</span>
            <span className="flex-1 border-t" />
          </div>
        )}
        {hasProvider && ownerCanUsePassword && !ownerForm && (
          <button
            type="button"
            className="text-link text-meta mt-3 -mx-1.5 flex min-h-11 items-center rounded-md px-1.5 font-medium disabled:opacity-50"
            disabled={busy}
            onClick={() => setOwnerForm(true)}
          >
            Owner? Sign in with your password
          </button>
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
                  ref={emailRef}
                  id="email"
                  type="email"
                  size="lg"
                  autoComplete="username"
                  required
                  aria-invalid={refused || undefined}
                  aria-describedby={login.isError ? errorId : undefined}
                  className="aria-invalid:focus-visible:border-destructive aria-invalid:focus-visible:outline-destructive"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  disabled={busy}
                />
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="password">
                {reset ? "New password" : "Password"}
              </Label>
              <Input
                ref={passwordRef}
                id="password"
                type="password"
                size="lg"
                autoComplete={reset ? "new-password" : "current-password"}
                required
                aria-invalid={refused || undefined}
                aria-describedby={login.isError ? errorId : undefined}
                className="aria-invalid:focus-visible:border-destructive aria-invalid:focus-visible:outline-destructive"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
              />
            </div>
            {login.isError && (
              <p id={errorId} role="alert" className="text-danger text-body rounded-control">
                {signInError(login.error)}
              </p>
            )}
            <Button
              type="submit"
              size="xl"
              variant={hasProvider && !filled ? "outline" : "make"}
              className={cn(
                "w-full whitespace-normal!",
                // The outline's edge is ink at 60%, so it reads as an edge in both themes (3:1 or more on the page).
                hasProvider && !filled && "rounded-full! border-foreground/60!",
              )}
              disabled={busy}
            >
              {login.isPending && <Loader2 aria-hidden className="motion-safe:animate-spin motion-reduce:hidden" />}
              {login.isPending
                ? reset
                  ? "Saving password…"
                  : "Signing in…"
                : reset
                  ? "Save password"
                  : "Sign in"}
            </Button>
            {!hasProvider && nameBox}
            {!reset && (
              <p className="text-muted-foreground text-meta text-pretty">
                Forgot your password? Ask the person who set up {instanceName || PRODUCT_NAME} for a reset link.
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
  instanceName,
  onBack,
  onGone,
}: {
  person: RememberedPerson;
  /** The instance's name, for the reset line. The product's name when none is set. */
  instanceName: string;
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
  useReportSignInBusy(attempt.isPending);
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
          className="text-3xl font-semibold tracking-[-0.02em] min-w-0 break-words outline-none"
        >
          {person.name}
        </h1>
      </div>
      <button
        type="button"
        className="text-link text-meta mt-3 -mx-1.5 flex min-h-11 items-center gap-1 rounded-md px-1.5 underline underline-offset-4 disabled:opacity-50"
        disabled={attempt.isPending}
        onClick={onBack}
      >
        <ChevronLeft aria-hidden className="size-4" />
        Not you? Choose someone else
      </button>
      <form
        className="mt-8 space-y-5"
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
            size="lg"
            autoComplete="current-password"
            required
            aria-label={`Password for ${person.name}`}
            aria-invalid={wrong || undefined}
            aria-describedby={wrong ? `${errorId} ${helpId}` : helpId}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={attempt.isPending}
            // On an invalid field the focus ring takes the error colour, so the error stays visible while typing.
            className="aria-invalid:focus-visible:border-destructive aria-invalid:focus-visible:outline-destructive"
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
          size="xl"
          variant="make"
          className="w-full whitespace-normal!"
          disabled={attempt.isPending || tooMany}
        >
          {attempt.isPending && <Loader2 aria-hidden className="motion-safe:animate-spin motion-reduce:hidden" />}
          {attempt.isPending ? "Signing in…" : "Sign in"}
        </Button>
        <p className="text-muted-foreground text-meta text-pretty">
          Forgot your password? Ask the person who set up {instanceName} for a reset link.
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
        className="text-3xl font-semibold tracking-[-0.02em] outline-none"
      >
        {name} can’t sign in from this list
      </h1>
      <p className="text-body mt-4">
        Try another way to sign in, or ask the person who invited you for help.
      </p>
      <div className="mt-8 flex flex-col gap-3 sm:flex-row">
        <Button size="xl" className="w-full sm:w-auto" onClick={onAnotherWay}>
          Sign in another way
        </Button>
        <Button size="xl" className="w-full sm:w-auto" variant="outline" onClick={onBack}>
          Back to the list
        </Button>
      </div>
    </>
  );
}
