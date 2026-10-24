"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { sessionIsClosed } from "@/lib/session-end";
import { unsavedSecretEntry } from "@/lib/unsaved-work";

/** Where a person tried to go while a new key was on screen and unsaved. */
type Leaving = { kind: "link"; href: string; opener: HTMLAnchorElement } | { kind: "back" };

/** The link a plain click would open in this tab, on this site, or null when the browser keeps the page. */
function sameTabLink(event: MouseEvent): { href: string; anchor: HTMLAnchorElement } | null {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
    return null;
  if (!(event.target instanceof Element)) return null;
  const anchor = event.target.closest("a[href]");
  if (!(anchor instanceof HTMLAnchorElement)) return null;
  if ((anchor.target && anchor.target !== "_self") || anchor.hasAttribute("download")) return null;
  const url = new URL(anchor.href);
  // Another site is left by the browser's own closing prompt (beforeunload), not by this guard.
  if (url.origin !== window.location.origin) return null;
  // The same page, such as a jump to a heading, keeps the key on screen.
  if (url.pathname === window.location.pathname && url.search === window.location.search) return null;
  return { href: `${url.pathname}${url.search}${url.hash}`, anchor };
}

/**
 * Asks before a new key on screen, unsaved, is dropped by an in-app link, by Back, or by closing the tab. The key stays
 * on screen while the question is open: a Back press is undone to the page the key is shown on, and a link is not
 * followed. Once the key is saved, nothing is asked.
 */
export function LeaveGuard() {
  const router = useRouter();
  const [leaving, setLeaving] = useState<Leaving | null>(null);
  // Set just before the one history.back() that a confirmed Back press makes, so that press is let through.
  const backConfirmed = useRef(false);

  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (!unsavedSecretEntry()) return;
      const link = sameTabLink(event);
      if (!link) return;
      // Stopped here, so neither the browser nor the router's link handler moves the page.
      event.preventDefault();
      event.stopPropagation();
      setLeaving({ kind: "link", href: link.href, opener: link.anchor });
    }

    function onPopState(event: PopStateEvent) {
      if (backConfirmed.current) {
        backConfirmed.current = false;
        return;
      }
      const entry = unsavedSecretEntry();
      if (!entry) return;
      const held = new URL(entry.url);
      // A Back press that only moves within this page (a hash) keeps the key on screen.
      if (held.pathname === window.location.pathname && held.search === window.location.search) return;
      // Stopped before the router sees it: the router would render the earlier page and the key would go with it.
      event.stopImmediatePropagation();
      window.history.pushState(entry.state, "", entry.url);
      setLeaving({ kind: "back" });
    }

    function onBeforeUnload(event: BeforeUnloadEvent) {
      // A session that is ending has already asked (the switch confirm), and its page replace must not ask twice.
      if (!unsavedSecretEntry() || sessionIsClosed()) return;
      event.preventDefault();
      event.returnValue = "";
    }

    document.addEventListener("click", onClick, true);
    window.addEventListener("popstate", onPopState, true);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      document.removeEventListener("click", onClick, true);
      window.removeEventListener("popstate", onPopState, true);
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, []);

  function leave() {
    if (leaving?.kind === "link") {
      router.push(leaving.href);
    } else if (leaving?.kind === "back") {
      backConfirmed.current = true;
      window.history.back();
    }
  }

  return (
    <ConfirmDialog
      open={leaving !== null}
      onOpenChange={(open) => {
        if (!open) setLeaving(null);
      }}
      title="Leave anyway?"
      description="You haven’t saved this key yet. It won’t be shown again."
      confirmLabel="Leave anyway"
      cancelLabel="Stay here"
      cancelVariant="outline"
      returnFocus={leaving?.kind === "link" ? leaving.opener : null}
      onConfirm={leave}
    />
  );
}
