"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { CircleAlert, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PRODUCT_NAME } from "@/lib/product";
import {
  continueFromOtherTab,
  getSessionScreen,
  retryPageCheck,
  retrySignOut,
  subscribeSession,
  type FailureReason,
  type OtherTabCause,
  type SessionScreen,
} from "@/lib/session-end";

/** The copy of DESIGN.md 4.3, 4.4 and 7. Shown in place of the app while this tab's session has ended. */
const SIGNING_OUT_DELAY_MS = 300;
const FAILED_HEADING = "Couldn’t finish signing out";
const FAILURE_BODY: Record<FailureReason, string> = {
  "no-answer":
    "This browser couldn’t reach the gateway, so you may still be signed in. Nothing of yours shows here until signing out finishes.",
  "gateway-error":
    "The gateway didn’t finish signing you out, so you may still be signed in. Nothing of yours shows here until it does.",
};
const WAY_OUT =
  "If this keeps happening, use Sign out everywhere on another device, or ask the person who runs Azizam.";
const OTHER_TAB_BODY: Record<OtherTabCause, string> = {
  switch: "You switched to someone else in another tab.",
  "sign-out": "You signed out in another tab.",
  server: "Azizam was signed out in another tab.",
};

/** The cover for this page's session, read from the session store: nothing while the session is live. */
export function SessionCoverFromStore({ header }: { header: ReactNode }) {
  const screen = useSyncExternalStore(subscribeSession, getSessionScreen, getSessionScreen);
  if (screen.kind === "idle") return null;
  return (
    <SessionCover
      screen={screen}
      header={header}
      onRetry={() => void (screen.kind === "unconfirmed" ? retryPageCheck() : retrySignOut())}
      onContinue={() => void continueFromOtherTab()}
    />
  );
}

export function SessionCover({
  screen,
  header,
  onRetry,
  onContinue,
}: {
  screen: Exclude<SessionScreen, { kind: "idle" }>;
  header: ReactNode;
  onRetry: () => void;
  onContinue: () => void;
}) {
  const title =
    screen.kind === "other-tab"
      ? `This tab is signed out · ${PRODUCT_NAME}`
      : screen.kind === "unconfirmed"
        ? `Checking your sign-in · ${PRODUCT_NAME}`
        : `Signing out · ${PRODUCT_NAME}`;
  useEffect(() => {
    document.title = title;
  }, [title]);
  return (
    <div className="min-h-dvh">
      {header}
      <main id="main-content" className="shell-content page-read">
        {screen.kind === "signing-out" && <SigningOut />}
        {screen.kind === "failed" && <Failed screen={screen} onRetry={onRetry} />}
        {screen.kind === "other-tab" && (
          <OtherTab cause={screen.cause} onContinue={onContinue} />
        )}
        {screen.kind === "unconfirmed" && <Unconfirmed onRetry={onRetry} />}
      </main>
    </div>
  );
}

/** Blank for the first 300 ms, so a quick sign-out shows nothing at all. */
function SigningOut() {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setShown(true), SIGNING_OUT_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, []);
  return (
    <div role="status" className="flex min-h-[40dvh] items-center justify-center">
      {shown && (
        <p className="text-muted-foreground flex items-center gap-2.5">
          <Loader2 className="size-4 animate-spin motion-reduce:hidden" aria-hidden="true" />
          Signing out…
        </p>
      )}
    </div>
  );
}

function Failed({
  screen,
  onRetry,
}: {
  screen: Extract<SessionScreen, { kind: "failed" }>;
  onRetry: () => void;
}) {
  const heading = useFocusOnEntry();
  return (
    <section aria-labelledby="session-heading" className="py-12">
      <CircleAlert className="text-muted-foreground mb-4 size-5" aria-hidden="true" />
      <h1 id="session-heading" ref={heading} tabIndex={-1} className="text-title text-balance">
        {FAILED_HEADING}
      </h1>
      {/* A live region: the way-out text, which the second failure adds, is read out when it appears. */}
      <div role="status">
        <p className="text-muted-foreground mt-4">{FAILURE_BODY[screen.reason]}</p>
        {screen.tries >= 2 && <p className="text-muted-foreground mt-4">{WAY_OUT}</p>}
      </div>
      <div className="mt-8">
        {/* aria-disabled, not disabled: the button keeps focus while the try runs. */}
        <Button
          aria-disabled={screen.trying}
          onClick={() => {
            if (!screen.trying) onRetry();
          }}
        >
          {screen.trying ? "Trying again…" : "Try again"}
        </Button>
      </div>
    </section>
  );
}

function OtherTab({ cause, onContinue }: { cause: OtherTabCause; onContinue: () => void }) {
  const heading = useFocusOnEntry();
  return (
    <section aria-labelledby="session-heading" className="py-12">
      <h1 id="session-heading" ref={heading} tabIndex={-1} className="text-title text-balance">
        This tab is signed out
      </h1>
      <p className="text-muted-foreground mt-4">{OTHER_TAB_BODY[cause]}</p>
      <div className="mt-8">
        <Button onClick={onContinue}>Continue</Button>
      </div>
    </section>
  );
}

/** The restore check got no answer for a page that showed a person: the page is held, and the check can be asked again. */
function Unconfirmed({ onRetry }: { onRetry: () => void }) {
  const heading = useFocusOnEntry();
  return (
    <section aria-labelledby="session-heading" className="py-12">
      <h1 id="session-heading" ref={heading} tabIndex={-1} className="text-title text-balance">
        Checking your sign-in…
      </h1>
      <div role="status">
        <p className="text-muted-foreground mt-4">
          Azizam can’t confirm yet that this page is still yours. Nothing of the previous page shows until it can.
        </p>
      </div>
      <div className="mt-8">
        <Button onClick={onRetry}>Try again</Button>
      </div>
    </section>
  );
}

/** Entering a screen moves focus to its heading (DESIGN.md 11). */
function useFocusOnEntry() {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return heading;
}
