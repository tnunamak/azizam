"use client";
import { useEffect, useState, type ReactNode } from "react";
import type { ConnectionStatus } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import { subscriptionLabel, subscriptionRowState } from "./subscription-state";

export function useSelectedSubscription() {
  const [kind, setKind] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setKind(new URLSearchParams(window.location.search).get("subscription"));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  return kind;
}
export function navigateSubscription(kind: string | null) {
  window.history.pushState({}, "", kind === null ? "/providers/" : `/providers/?subscription=${encodeURIComponent(kind)}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
export function SubscriptionDetail({ sub, children, stale }: { sub?: ConnectionStatus; children?: ReactNode; stale: boolean }) {
  return <div className="space-y-4">
    <a className="underline underline-offset-2" href="/providers/" onClick={(event) => { event.preventDefault(); navigateSubscription(null); }}>Back to Providers</a>
    <h2 className="text-title">{sub ? subscriptionLabel(sub) : "Plan not found"}</h2>
    {stale && <p role="alert">Status could not refresh. Showing previously loaded data.</p>}
    {sub && <><p>{subscriptionRowState(sub).detail}</p><p>{sub.account_email}</p>
      <p>Last successful request: {sub.last_ok_at ? formatRelativeTime(sub.last_ok_at) : "Not recorded"}.</p>
      {sub.last_error && <p>Last error: {sub.last_error.code}.</p>}
      {children}</>}
  </div>;
}
