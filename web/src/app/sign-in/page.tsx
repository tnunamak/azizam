"use client";
import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
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
  | { kind: "plain"; fromList: boolean };

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
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    const address = window.location.search;
    const params = new URLSearchParams(address);
    const after = afterFrom(address);
    const error = params.get("error");
    setArrival({
      after,
      leaving: after === "switch" || after === "sign-out" ? leavingTileFromRecord() : null,
      signedOut: readHandoff()?.state === "SIGNED_OUT",
      requestedReturn: params.get("return_to"),
      providerError:
        error === "provider" || error === "no_sign_in_time" || error === "not_fresh" || error === "tile_unknown"
          ? error
          : null,
      providerSignOut: params.get("provider_sign_out") === "1",
      reset: new URLSearchParams(window.location.hash.slice(1)).get("reset") || "",
    });
    setNotice(after ? { kind: "after" } : null);
    if (after) {
      // The ending is said once: a reload, or the next action, shows no line.
      const url = new URL(window.location.href);
      url.searchParams.delete("after");
      window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    }
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
  const options = useQuery({
    queryKey: ["auth", "options", returnTo],
    enabled: returnTo !== null,
    queryFn: () =>
      authFetch<SignInOptions>(`/auth/api/options?return_to=${encodeURIComponent(returnTo!)}`),
  });
  const setup = useQuery({
    queryKey: ["auth", "setup"],
    queryFn: () => authFetch<{ owner_exists: boolean }>("/setup/api/state"),
  });
  // The sign-out screen for a refused sign-in (S8) shows no names, so it does not read the list.
  const listNeeded = arrival !== null && arrival.providerError !== "not_fresh";
  const remembered = useQuery({
    queryKey: ["auth", "remembered"],
    enabled: listNeeded,
    queryFn: rememberedList,
  });

  const retry = () => {
    void options.refetch();
    void setup.refetch();
    void remembered.refetch();
  };

  const loading =
    arrival === null || options.isPending || setup.isPending || (listNeeded && remembered.isPending);
  const unavailable = options.isError || setup.isError;
  // A list that cannot be read is said so (S4e). Any other refusal of the list falls back to the plain form.
  const listUnreachable = remembered.isError && isNetworkFailure(remembered.error);
  const reset = arrival?.reset ?? "";
  const firstRun = setup.data?.owner_exists === false && !reset;
  const instanceName = options.data?.instance_name?.trim();
  const providerName = providerNameFor(options.data?.oidc_label?.trim());
  // Until the list says otherwise, the name box is on: the sign-in keeps the name unless the owner remembers nobody.
  const rememberOn = remembered.data?.remember !== false;
  const everyone: RememberedPerson[] = remembered.data?.people ?? [];
  const people = everyone.filter((person) => !removed.has(person.tile));
  const nameOf = (tile: string) => everyone.find((person) => person.tile === tile)?.name ?? "";

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
      : arrival?.providerError === "not_fresh"
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

  const card = (content: ReactNode) => (
    <section aria-labelledby="sign-in-title" className="mx-auto w-full max-w-md py-6 sm:py-12">
      <div className="bg-card rounded-panel border p-6 sm:p-8">{content}</div>
    </section>
  );

  if (arrival === null || loading) {
    return card(
      <>
        <h1 id="sign-in-title" className="text-title font-semibold tracking-tight">
          {heading}
        </h1>
        <div className="mt-8">
          <p role="status" className="text-muted-foreground text-body">
            Getting sign-in ready…
          </p>
        </div>
      </>,
    );
  }

  if (unavailable || listUnreachable) {
    return card(
      <>
        <h1 id="sign-in-title" className="text-title font-semibold tracking-tight">
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

  if (arrival.providerError === "not_fresh") {
    return card(
      <NotFreshScreen
        provider={providerName}
        signOut={arrival.providerSignOut}
        signInAgain={options.data ? withRemember(options.data.oidc_url, rememberOn) : "/sign-in/"}
        onBack={leaveNotFresh}
      />,
    );
  }

  if (firstRun) {
    return card(
      <>
        <h1 id="sign-in-title" className="text-title font-semibold tracking-tight">
          {heading}
        </h1>
        <div className="mt-8 space-y-6">
          <p className="text-muted-foreground text-body">
            Create the first owner account to get started. If you were
            invited, ask your host to finish setup.
          </p>
          <Button className="h-auto min-h-11 w-full py-2 whitespace-normal!" asChild>
            <a href="/setup/">Set up your gateway</a>
          </Button>
        </div>
      </>,
    );
  }

  const plain = (fromList: boolean) => (
    <PlainSignIn
      data={options.data}
      reset={reset}
      returnTo={returnTo ?? "/"}
      rememberOn={rememberOn}
      fromList={fromList}
      onBack={() => {
        setNotice(null);
        setView(null);
      }}
      providerError={arrival.providerError}
    />
  );
  const statusLine = status && (
    <StatusLine
      status={status}
      onForget={forgetFromStatus}
    />
  );

  if (reset) return card(plain(false));

  switch (current.kind) {
    case "list":
      return card(
        <Chooser
          people={ordered}
          providerName={providerName}
          status={status}
          opening={opening}
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
          onSomeoneElse={() => {
            setNotice(null);
            setActionError(null);
            setView({ kind: "plain", fromList: true });
          }}
          onForgetMode={() => {
            setNotice(null);
            setActionError(null);
            setView({ kind: "forget" });
          }}
          onForgetFromStatus={forgetFromStatus}
        />,
      );
    case "forget": {
      const forgot = notice?.kind === "forgot" ? statusFor("forgot", { name: notice.name }) : null;
      return card(
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
      if (!who) return card(plain(false));
      return card(
        <PersonPassword
          person={who}
          onBack={() => {
            setNotice(null);
            setView(null);
          }}
          onGone={markGone}
        />,
      );
    }
    case "gone":
      return card(
        <GoneNotice
          name={nameOf(current.tile)}
          onAnotherWay={() => setView({ kind: "plain", fromList: true })}
          onBack={() => setView(null)}
        />,
      );
    case "plain":
      return card(
        <>
          {statusLine}
          {plain(current.fromList)}
        </>,
      );
  }
}
