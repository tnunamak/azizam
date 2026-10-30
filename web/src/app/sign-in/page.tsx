"use client";
import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { BrandMark } from "@/components/product/brand-mark";
import { Button } from "@/components/ui/button";
import { navigate } from "@/lib/navigate";
import { isNetworkFailure } from "@/lib/network-error";
import { PRODUCT_NAME } from "@/lib/product";
import type { RememberedPerson } from "@/lib/remembered-people";
import { readHandoff } from "@/lib/session-end";
import {
  authFetch,
  forgetRemembered,
  rememberedList,
  type SignInOptions,
} from "./auth-api";
import { Chooser, ForgetList, StatusLine } from "./chooser";
import {
  GoneNotice,
  NotFreshScreen,
  PersonPassword,
  PlainSignIn,
  ProviderNotices,
  providerNameFor,
  withRemember,
  type ProviderError,
} from "./person-sign-in";
import {
  afterFrom,
  notFreshTitle,
  orderPeople,
  returnToFor,
  statusFor,
  type After,
  type LeftPerson,
  type Status,
} from "./sign-in-status";

/** What the page learns from its address and this browser's record, once, on arrival. */
type Arrival = {
  after: After | null;
  /** The tile of the person who just left, as this browser recorded it for a switch or a sign-out. */
  leaving: string | null;
  /** This browser signed someone out, and no one has signed in since. */
  signedOut: boolean;
  requestedReturn: string | null;
  providerError: ProviderError;
  providerSignOut: boolean;
  reset: string;
};

/** The screen in the card. Null means the default: the list when two or more people are remembered. */
type View =
  | { kind: "list" }
  | { kind: "forget" }
  | { kind: "person"; tile: string }
  | { kind: "gone"; tile: string }
  /** `owner`: open the owner's password form at once, from the list's owner link. */
  | { kind: "plain"; fromList: boolean; owner?: boolean };

/** What the page last reported after the person's own action: how the session ended, or what was forgotten. */
type Notice =
  | { kind: "after" }
  | { kind: "forgot"; name: string }
  | { kind: "all-forgotten" };

/** The tile of the person who left, from the record the sign-out wrote to this tab (SECURITY.md section 4). */
function leavingTileFromRecord(): string | null {
  try {
    const tile = (JSON.parse(sessionStorage.getItem("azizam.handoff.last") ?? "{}") as { tile?: unknown }).tile;
    return typeof tile === "string" && tile !== "" ? tile : null;
  } catch {
    return null;
  }
}

/**
 * Reads the address and this browser's record. It runs in an effect, after the page has committed: an in-app
 * navigation renders the sign-in page before the router has written its address into the window, so a read during
 * render would see the page the person came from.
 */
function readArrival(): Arrival {
  const address = window.location.search;
  const params = new URLSearchParams(address);
  const after = afterFrom(address);
  const error = params.get("error");
  return {
    after,
    leaving: after === "switch" || after === "sign-out" ? leavingTileFromRecord() : null,
    signedOut: readHandoff()?.state === "SIGNED_OUT",
    // "/" is the sign-in page as well: a person who signs in there comes back to that same address.
    requestedReturn:
      params.get("return_to") ?? (window.location.pathname === "/" ? `${window.location.pathname}${address}` : null),
    providerError:
      error === "provider" || error === "no_sign_in_time" || error === "not_fresh" || error === "tile_unknown"
        ? error
        : null,
    providerSignOut: params.get("provider_sign_out") === "1",
    reset: new URLSearchParams(window.location.hash.slice(1)).get("reset") || "",
  };
}

/** A view the list or forget mode cannot show with the names left (two for the list, one for forget mode). */
function fitView(view: View, count: number): View {
  if (view.kind === "list" && count < 2) return { kind: "plain", fromList: false };
  if (view.kind === "forget" && count < 1) return { kind: "plain", fromList: false };
  return view;
}

function titleFor(view: View, heading: { reset: boolean; firstRun: boolean }, nameOf: (tile: string) => string): string {
  if (heading.firstRun) return "Set up Azizam · Azizam";
  if (heading.reset) return "Choose a new password · Azizam";
  switch (view.kind) {
    case "list":
      return "Who’s signing in? · Azizam";
    case "forget":
      return "Forget someone on this browser · Azizam";
    case "person":
      return `${nameOf(view.tile)} · Sign in · Azizam`;
    case "gone":
      return `${nameOf(view.tile)} can’t sign in from this list · Azizam`;
    case "plain":
      return "Sign in · Azizam";
  }
}

export default function SignInPage() {
  const [arrival, setArrival] = useState<Arrival | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [removed, setRemoved] = useState<Set<string>>(() => new Set());
  const [opening, setOpening] = useState<string | null>(null);
  const [openingElse, setOpeningElse] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Until this runs the page is the loading state, the same as the server's render. Its requests start in the render
  // that follows, together.
  useEffect(() => {
    const next = readArrival();
    setArrival(next);
    setNotice(next.after ? { kind: "after" } : null);
    if (!next.after) return;
    // The ending is said once: a reload, or the next action, shows no line.
    const url = new URL(window.location.href);
    url.searchParams.delete("after");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, []);

  // A link from an earlier person's page is dropped after a switch or a sign-out (DESIGN.md 4.10).
  const returnTo = arrival
    ? returnToFor({
        requested: arrival.requestedReturn,
        after: arrival.after,
        signedOut: arrival.signedOut,
        fromTile: false,
      })
    : null;
  const {
    data: options,
    isError: optionsFailed,
    isPending: optionsPending,
    refetch: refetchOptions,
  } = useQuery({
    queryKey: ["auth", "options", returnTo],
    enabled: returnTo !== null,
    queryFn: () =>
      authFetch<SignInOptions>(`/auth/api/options?return_to=${encodeURIComponent(returnTo!)}`),
  });
  const {
    data: setup,
    isError: setupFailed,
    isPending: setupPending,
    refetch: refetchSetup,
  } = useQuery({
    queryKey: ["auth", "setup"],
    enabled: arrival !== null,
    queryFn: () => authFetch<{ owner_exists: boolean }>("/setup/api/state"),
  });
  // The refused-sign-in screen (S8) is inert until the gateway reports fresh sign-in (SignInOptions.fresh_sign_in). It
  // shows no names, so it does not read the list.
  const refusedNotFresh = arrival?.providerError === "not_fresh" && options?.fresh_sign_in === true;
  const listNeeded = arrival !== null && !refusedNotFresh;
  const {
    data: remembered,
    error: rememberedError,
    isError: rememberedFailed,
    isPending: rememberedPending,
    refetch: refetchRemembered,
  } = useQuery({
    queryKey: ["auth", "remembered"],
    enabled: listNeeded,
    queryFn: rememberedList,
  });

  const retry = () => {
    void refetchOptions();
    void refetchSetup();
    void refetchRemembered();
  };

  const loading =
    arrival === null || optionsPending || setupPending || (listNeeded && rememberedPending);
  const unavailable = optionsFailed || setupFailed;
  // A list that cannot be read is said so (S4e). Any other refusal of the list falls back to the plain form.
  const listUnreachable = rememberedFailed && isNetworkFailure(rememberedError);
  const reset = arrival?.reset ?? "";
  const firstRun = setup?.owner_exists === false && !reset;
  const instanceName = options?.instance_name?.trim();
  const providerName = providerNameFor(options?.oidc_label?.trim());
  // Until the list says otherwise, the name box is on: the sign-in keeps the name unless the owner remembers nobody.
  const rememberOn = remembered?.remember !== false;
  const everyone: RememberedPerson[] = remembered?.people ?? [];
  const people = everyone.filter((person) => !removed.has(person.tile));
  const nameOf = (tile: string) => everyone.find((person) => person.tile === tile)?.name ?? "";
  // Single sign-on is the only way in. The owner's password, when allowed, stays one link away from the list.
  const ssoOnly = Boolean(options?.oidc) && !options?.built_in;
  const ownerPasswordOffered = ssoOnly && options?.owner_password !== false;

  const current: View = reset
    ? { kind: "plain", fromList: false }
    : fitView(
        view ?? (people.length >= 2 ? { kind: "list" } : { kind: "plain", fromList: false }),
        people.length,
      );
  const ordered = orderPeople(people, arrival?.after === "switch" ? arrival.leaving : null);
  const leaver = people.find((person) => person.tile === arrival?.leaving);
  const leftPerson: LeftPerson | null = leaver ? { tile: leaver.tile, name: leaver.name } : null;

  const status = ((): Status | null => {
    if (notice === null) return null;
    if (notice.kind === "forgot") return statusFor("forgot", { name: notice.name });
    if (notice.kind === "all-forgotten") return statusFor("all-forgotten");
    if (!arrival?.after) return null;
    return statusFor(arrival.after, { leaver: leftPerson, provider: providerName });
  })();

  const heading = reset
    ? "Choose a new password"
    : firstRun
      ? "Set up Azizam"
      : `Sign in to ${instanceName || PRODUCT_NAME}`;
  const title =
    loading || unavailable || listUnreachable
      ? "Sign in · Azizam"
      : refusedNotFresh
        ? `${notFreshTitle(providerName)} · Azizam`
        : titleFor(current, { reset: Boolean(reset), firstRun }, nameOf);
  useEffect(() => {
    document.title = title;
  }, [title]);

  // Every action ends the line from the last one, so it does not outlive the thing it reported.
  function chooseRow(person: RememberedPerson) {
    setNotice(null);
    setActionError(null);
    if (person.method === "sso") {
      // The provider asks for the person's password again. The tile carries the name, and the way back is Home.
      setOpening(person.tile);
      navigate.to(`/login?tile=${encodeURIComponent(person.tile)}&remember=1`);
      return;
    }
    setView({ kind: "person", tile: person.tile });
  }

  // With single sign-on as the only way in, "Someone else" is the provider: one click, with the same name choice as the
  // form's button. With a password form too, it opens the form (DESIGN.md 4.9).
  function someoneElse() {
    setNotice(null);
    setActionError(null);
    if (ssoOnly && options) {
      setOpeningElse(true);
      navigate.to(withRemember(options.oidc_url, rememberOn));
      return;
    }
    setView({ kind: "plain", fromList: true });
  }

  function forgetPerson(person: { tile: string; name: string }) {
    setActionError(null);
    setRemoved((before) => new Set(before).add(person.tile));
    forgetRemembered(person.tile).catch(() => {
      setRemoved((before) => {
        const after = new Set(before);
        after.delete(person.tile);
        return after;
      });
      setActionError("Couldn’t reach the gateway. Try again in a moment.");
    });
  }

  function forgetInMode(person: RememberedPerson) {
    forgetPerson(person);
    if (people.length <= 1) {
      setNotice({ kind: "all-forgotten" });
      setView({ kind: "plain", fromList: false });
    } else {
      setNotice({ kind: "forgot", name: person.name });
    }
  }

  function forgetFromStatus(person: LeftPerson) {
    forgetPerson(person);
    setNotice({ kind: "forgot", name: person.name });
    setView(null);
  }

  function markGone(tile: string) {
    setRemoved((before) => new Set(before).add(tile));
    setView({ kind: "gone", tile });
  }

  // Back leaves the sign-out screen for the sign-in page. The address forgets the refusal too, so a reload does not
  // bring the screen back.
  function leaveNotFresh() {
    setArrival((before) => (before ? { ...before, providerError: null, providerSignOut: false } : before));
    const url = new URL(window.location.href);
    url.searchParams.delete("error");
    url.searchParams.delete("provider_sign_out");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }

  // The column every state shares: the mark, then the state's own heading and content. The frame around it is
  // SignInFrame (components/shell); this page is the same whether it is reached at /sign-in/ or at "/".
  const column = (content: ReactNode) => (
    <section aria-labelledby="sign-in-title" className="w-full max-w-[400px]">
      <BrandMark className="mb-6 size-11!" />
      {content}
    </section>
  );

  if (arrival === null || loading) {
    // The loaded single sign-on page's own rhythm, so the column does not move when the options arrive: the heading,
    // the button, the name box under it (16 above, then 58 for its row and its two-line helper), and the owner's link.
    // The heading text is fixed: the address may name a different page, and the server render has no address.
    return column(
      <>
        <h1 id="sign-in-title" className="sr-only">
          Sign in
        </h1>
        {/* Quiet blocks, not the shell's pulsing skeletons: the sign-in page shows none of the app's loading chrome. */}
        <div aria-hidden="true">
          <div className="bg-muted rounded-control h-9 w-72 max-w-full" />
        </div>
        <div aria-hidden="true" className="mt-8">
          <div className="bg-muted rounded-control h-12 w-full" />
          <div className="mt-4 h-[58px]" />
          <div className="mt-3 h-11" />
        </div>
        <p role="status" className="sr-only">
          Getting sign-in ready…
        </p>
      </>,
    );
  }

  if (unavailable || listUnreachable) {
    return column(
      <>
        <h1 id="sign-in-title" className="text-3xl font-semibold tracking-[-0.02em]">
          {heading}
        </h1>
        <div className="mt-8 space-y-4">
          <p role="alert" className="text-body">
            {unavailable
              ? "Couldn’t load sign-in. Check your connection and try again."
              : "Couldn’t reach the gateway."}
          </p>
          <Button className="min-h-11" variant="outline" onClick={retry}>
            Try again
          </Button>
        </div>
      </>,
    );
  }

  if (refusedNotFresh) {
    return column(
      <NotFreshScreen
        provider={providerName}
        signOut={arrival.providerSignOut}
        signInAgain={options ? withRemember(options.oidc_url, rememberOn) : "/sign-in/"}
        onBack={leaveNotFresh}
      />,
    );
  }

  if (firstRun) {
    return column(
      <>
        <h1 id="sign-in-title" className="text-3xl font-semibold tracking-[-0.02em]">
          {heading}
        </h1>
        <div className="mt-8 space-y-6">
          <p className="text-muted-foreground text-body">
            Create the first owner account to get started. If you were
            invited, ask your host to finish setup.
          </p>
          <Button size="xl" className="w-full whitespace-normal!" asChild>
            <a href="/setup/">Set up your gateway</a>
          </Button>
        </div>
      </>,
    );
  }

  const plain = (fromList: boolean, owner = false) => (
    <PlainSignIn
      data={options}
      reset={reset}
      returnTo={returnTo ?? "/"}
      rememberOn={rememberOn}
      fromList={fromList}
      onBack={() => {
        setNotice(null);
        setView(null);
      }}
      providerError={arrival.providerError}
      openOwnerForm={owner}
      // The provider's trip replaces the page's last line, so only one status region is on screen.
      onOpening={() => setNotice(null)}
    />
  );
  const statusLine = status && (
    <StatusLine
      status={status}
      onForget={forgetFromStatus}
    />
  );

  if (reset) return column(plain(false));

  switch (current.kind) {
    case "list":
      return column(
        <Chooser
          people={ordered}
          providerName={providerName}
          instanceName={instanceName || PRODUCT_NAME}
          status={status}
          opening={opening}
          openingElse={openingElse}
          alerts={
            <>
              {actionError && (
                <p role="alert" className="text-danger text-body mb-4">
                  {actionError}
                </p>
              )}
              <ProviderNotices error={arrival.providerError} />
            </>
          }
          onChoose={chooseRow}
          onSomeoneElse={someoneElse}
          onForgetMode={() => {
            setNotice(null);
            setActionError(null);
            setView({ kind: "forget" });
          }}
          onForgetFromStatus={forgetFromStatus}
          onOwnerPassword={
            ownerPasswordOffered
              ? () => {
                  setNotice(null);
                  setActionError(null);
                  setView({ kind: "plain", fromList: true, owner: true });
                }
              : undefined
          }
        />,
      );
    case "forget": {
      const forgot = notice?.kind === "forgot" ? statusFor("forgot", { name: notice.name }) : null;
      return column(
        <ForgetList
          people={people}
          providerName={providerName}
          notice={forgot}
          error={actionError}
          onForget={forgetInMode}
          onDone={() => {
            setNotice(null);
            setActionError(null);
            setView(null);
          }}
        />,
      );
    }
    case "person": {
      const who = people.find((person) => person.tile === current.tile);
      if (!who) return column(plain(false));
      return column(
        <PersonPassword
          person={who}
          instanceName={instanceName || PRODUCT_NAME}
          onBack={() => {
            setNotice(null);
            setView(null);
          }}
          onGone={markGone}
        />,
      );
    }
    case "gone":
      return column(
        <GoneNotice
          name={nameOf(current.tile)}
          onAnotherWay={() => setView({ kind: "plain", fromList: true })}
          onBack={() => setView(null)}
        />,
      );
    case "plain":
      return column(
        <>
          {statusLine}
          {plain(current.fromList, current.owner)}
        </>,
      );
  }
}
