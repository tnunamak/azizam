"use client";

import Link from "next/link";
import { CopyField } from "@/components/product/copy-field";
import { Fragment, useEffect, useId, useState } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import reasoningSchema from "../../../../registry/defaults/reasoning.schema.json";
import { ListCard } from "@/components/product/list-card";
import { Callout } from "@/components/product/status-ui";
import { Button } from "@/components/ui/button";
import { OverflowMenu, OverflowMenuItem } from "@/components/ui/overflow-menu";
import { adminFetch, type ConnectionStatus } from "@/lib/api";
import { toast } from "@/lib/toast";
import { SubscriptionConnectDialog } from "./subscription-connect-dialog";
import { SubscriptionDisconnectDialog } from "./subscription-disconnect-dialog";
import { SubscriptionRow } from "./subscription-row";
import {
  planUsageDetail,
  planUsagePageUrl,
  routeChainLines,
  sameRouteChain,
  subscriptionLabel,
  subscriptionModelNames,
  type SubscriptionAction,
} from "./subscription-state";

export const SUBSCRIPTIONS_KEY = ["subscriptions"] as const;
const EFFORTS = reasoningSchema.properties.default_effort.enum.filter((effort): effort is string => effort !== null);
const SUBSCRIPTION_CHECK_KEY = ["subscription-check"] as const;

/** The subscriptions list. It always loads for the owner, so a kind that was never connected still has a row to connect from. */
export function useSubscriptions(enabled: boolean) {
  return useQuery({
    queryKey: SUBSCRIPTIONS_KEY,
    queryFn: async () => {
      const result = await adminFetch<{ subscriptions?: ConnectionStatus[] } | null>("/admin/api/subscriptions");
      if (!Array.isArray(result?.subscriptions)) throw new Error("Invalid subscriptions response.");
      return { subscriptions: result.subscriptions };
    },
    enabled,
    retry: false,
  });
}

export function SubscriptionsSection({ enabled }: { enabled: boolean }) {
  const queryClient = useQueryClient();
  const { data, isError, refetch } = useSubscriptions(enabled);
  const [connecting, setConnecting] = useState<ConnectionStatus | null>(null);
  const [disconnecting, setDisconnecting] = useState<ConnectionStatus | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: SUBSCRIPTIONS_KEY });
  const check = useMutation({
    mutationKey: SUBSCRIPTION_CHECK_KEY,
    mutationFn: (sub: ConnectionStatus) =>
      adminFetch(`/admin/api/subscriptions/${encodeURIComponent(sub.kind)}/refresh-models`, { method: "POST", body: {} }),
    onSuccess: refresh,
    onError: (error, sub) =>
      toast.error(error instanceof Error ? error.message : `Could not check ${subscriptionLabel(sub)}. Try again.`),
  });
  const pendingCheckKinds = useMutationState({
    filters: { mutationKey: SUBSCRIPTION_CHECK_KEY, exact: true, status: "pending" },
    select: (mutation) => (mutation.state.variables as ConnectionStatus | undefined)?.kind,
  });

  const saveLevel = useMutation({
    mutationFn: ({ kind, top_effort }: { kind: string; top_effort: string | null }) => adminFetch(
      `/admin/api/subscriptions/${encodeURIComponent(kind)}/reasoning`,
      { method: "PATCH", body: { top_effort } },
    ),
    onSuccess: refresh,
    onError: (error) => toast.error(error instanceof Error ? error.message : "Could not save the reasoning level."),
  });

  const rows = data?.subscriptions;
  useEffect(() => {
    if (opened !== null && rows && !rows.some((sub) => sub.kind === opened && sub.status === "connected")) {
      setOpened(null);
    }
  }, [opened, rows]);

  if (!enabled) return null;
  if (isError && !rows) {
    return (
      <section aria-label="Subscriptions" className="space-y-3">
        <h2 className="text-label">Subscriptions</h2>
        {/* Not an alert: in an outage the page shows the one alert, and this section only says what it lacks. */}
        <p>Subscriptions couldn&apos;t load.</p>
        <Button size="sm" variant="outline" onClick={() => void refetch()}>Try again</Button>
      </section>
    );
  }
  if (!rows || rows.length === 0) return null;

  const isChecking = (sub: ConnectionStatus) => pendingCheckKinds.includes(sub.kind);
  const isExpanded = (sub: ConnectionStatus) => sub.status === "connected" && opened === sub.kind;
  const requestCheck = (sub: ConnectionStatus) => {
    if (!isChecking(sub)) check.mutate(sub);
  };

  function run(sub: ConnectionStatus, action: SubscriptionAction) {
    if (action === "connect" || action === "reconnect") setConnecting(sub);
    else if (action === "check") requestCheck(sub);
    else setOpened((current) => (current === sub.kind ? null : sub.kind));
  }

  return (
    <section className="space-y-3" aria-label="Subscriptions">
      <h2 className="text-label">Subscriptions</h2>
      {notice && (
        <Callout severity="attention" title="Disconnected here" as="div">
          <p>{notice}</p>
        </Callout>
      )}
      <ListCard label="Subscriptions">
        {rows.map((sub) => (
          <Fragment key={sub.kind}>
            <SubscriptionRow
              sub={sub}
              busy={isChecking(sub)}
              expanded={isExpanded(sub)}
              onAction={(action) => run(sub, action)}
              menu={
                sub.status === "not_connected" || sub.status === "disconnected" ? undefined : (
                  <OverflowMenu label={`Actions for ${subscriptionLabel(sub)}`}>
                    {sub.status === "connected" && !isChecking(sub) && (
                      <OverflowMenuItem onSelect={() => requestCheck(sub)}>Check now</OverflowMenuItem>
                    )}
                    <OverflowMenuItem danger onSelect={() => setDisconnecting(sub)}>
                      Disconnect
                    </OverflowMenuItem>
                  </OverflowMenu>
                )
              }
            />
            {isExpanded(sub) && (
              <SubscriptionDetails
                sub={sub}
                subscriptions={rows}
                onTopEffortChange={(top_effort) => saveLevel.mutate({ kind: sub.kind, top_effort })}
                savingTopEffort={saveLevel.isPending && saveLevel.variables.kind === sub.kind}
                onCheck={isChecking(sub) ? undefined : () => requestCheck(sub)}
              />
            )}
          </Fragment>
        ))}
      </ListCard>
      {connecting && (
        <SubscriptionConnectDialog
          open
          onOpenChange={(open) => !open && setConnecting(null)}
          kind={connecting}
          current={connecting}
          reconnect={connecting.status === "needs_reconnect" || connecting.status === "connected"}
          onConnected={() => {
            setNotice(null);
            toast.success(`${subscriptionLabel(connecting)} connected.`);
            void refresh();
          }}
        />
      )}
      {disconnecting && (
        <SubscriptionDisconnectDialog
          open
          onOpenChange={(open) => !open && setDisconnecting(null)}
          sub={disconnecting}
          onDisconnected={(view) => {
            const name = disconnecting.display_name || disconnecting.kind;
            setNotice(
              view.revoked_upstream === false
                ? `${name} could not be reached, so end the sign-in in ${name}'s settings too.`
                : null,
            );
            toast.success(`${subscriptionLabel(disconnecting)} disconnected.`);
            void refresh();
          }}
        />
      )}
    </section>
  );
}

/**
 * The models one connected subscription offers, with the reasoning efforts each advertises, the name that pins a
 * request to it, and the steps a plain-name request walks (read-only, from the router's own chain).
 */
export function SubscriptionDetails({
  sub,
  subscriptions,
  onTopEffortChange,
  savingTopEffort = false,
  onCheck,
}: {
  sub: ConnectionStatus;
  subscriptions: ConnectionStatus[];
  onTopEffortChange?: (effort: string | null) => void;
  savingTopEffort?: boolean;
  onCheck?: () => void;
}) {
  const levelId = useId();
  const topEffortIndex = EFFORTS.indexOf(sub.top_effort ?? "");
  const unknownTopEffort = !!sub.top_effort && topEffortIndex < 0;
  const levels = EFFORTS.filter((effort) => effort === sub.top_effort || sub.model_details?.some(
    (model) => model.efforts.includes(effort),
  ));
  const efforts = new Map((sub.model_details ?? []).map((model) => [model.slug, model.efforts]));
  // The router reads only the levels it knows, in their order; an unknown word never decides the level used.
  const knownEfforts = new Map((sub.model_details ?? []).map(
    (model) => [model.slug, EFFORTS.filter((effort) => model.efforts.includes(effort))],
  ));
  const pins = new Map((sub.routes ?? []).map((route) => [route.logical_model, route.id]));
  const chains = new Map((sub.model_chains ?? []).map((chain) => [chain.slug, chain]));
  const slugs = subscriptionModelNames(sub) ?? [];
  const usagePageUrl = planUsagePageUrl(sub.usage_page_url);
  const shown = slugs.map((slug) => chains.get(slug)).filter((chain) => chain !== undefined);
  // One list for all models when every model walks the same steps; otherwise one list under each model.
  const shared =
    shown.length > 1 && shown.length === slugs.length && shown.every((chain) => !chain.direct && chain.steps.length > 0 && sameRouteChain(chain, shown[0]))
      ? shown[0]
      : null;
  const walked = shown.some((chain) => !chain.direct && chain.steps.length > 0);
  const fallback = shown.some((chain) => !chain.direct && chain.steps.some((step) => step.paid_fallback));
  const steps = (label: string, lines: string[]) => (
    <ol aria-label={label} className="text-muted-foreground space-y-0.5">
      {lines.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ol>
  );
  return (
    <li className="text-meta space-y-2 px-4 py-3">
      {slugs.length > 0 ? (
        <p>
          {slugs.length} {slugs.length === 1 ? "model" : "models"}. Choose who can use them in{" "}
          <Link href="/people/" className="underline underline-offset-2">
            access plans
          </Link>
          .
        </p>
      ) : (
        <>
          <p>{sub.models != null ? "No models listed." : "No model list yet."}</p>
          {sub.status === "connected" && sub.models == null && onCheck && (
            <Button size="sm" variant="outline" onClick={onCheck}>Check now</Button>
          )}
        </>
      )}
      {sub.status === "connected" && (
        <div className="space-y-1">
          <label htmlFor={levelId} className="block">Top reasoning level</label>
          <select
            id={levelId}
            value={sub.top_effort ?? ""}
            // aria-disabled, not disabled: a disabled control drops keyboard focus while the save runs.
            aria-disabled={savingTopEffort}
            onChange={(event) => {
              if (!savingTopEffort) onTopEffortChange?.(event.target.value || null);
            }}
            className="border-input rounded-control min-h-11 border bg-background px-3 py-2"
          >
            <option value="">No limit</option>
            {unknownTopEffort && <option value={sub.top_effort!} disabled>Unrecognized level: {sub.top_effort}</option>}
            {levels.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
          </select>
          <p role="status" data-testid="top-effort-status" className="text-meta text-muted-foreground">
            {savingTopEffort ? "Saving…" : ""}
          </p>
        </div>
      )}
      {sub.status === "connected" && (
        <div aria-label="Plan usage" role="group" className="space-y-0.5">
          {planUsageDetail(sub).map((line, index) => (
            <p key={`${index}-${line}`}>{line}</p>
          ))}
          {usagePageUrl && (
            <p>
              <a href={usagePageUrl} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                Open {sub.display_name || sub.kind} usage<span className="sr-only"> (opens in a new tab)</span>
              </a>
            </p>
          )}
        </div>
      )}
      {slugs.length > 0 && (
        <ul className="space-y-2" aria-label={`Models of ${subscriptionLabel(sub)}`}>
          {slugs.map((slug) => (
            <li key={slug} className="space-y-1">
              <p>
                <span className="text-foreground">{slug}</span>
                {efforts.get(slug)?.length ? ` · ${efforts.get(slug)!.join(" · ")}` : ""}
              </p>
              {topEffortIndex >= 0 && knownEfforts.get(slug)?.length && !knownEfforts.get(slug)!.some(
                (effort) => EFFORTS.indexOf(effort) <= topEffortIndex,
              ) ? (
                <p className="text-muted-foreground">
                  Uses {knownEfforts.get(slug)![0]}: this model has no level at or below {sub.top_effort}.
                </p>
              ) : null}
              {pins.has(slug) && (
                <CopyField
                  label={`Pinned name for ${slug}`}
                  value={pins.get(slug)!}
                  buttonLabel={`Copy the pinned name for ${slug}`}
                />
              )}
              {!shared && chains.has(slug) && steps(`Steps for ${slug}`, routeChainLines(chains.get(slug)!, subscriptions))}
            </li>
          ))}
        </ul>
      )}
      {shared && (
        <div className="space-y-1">
          <p>Every model walks these steps when a request uses its plain name:</p>
          {steps("Steps for every model", routeChainLines(shared, subscriptions))}
        </div>
      )}
      {walked && (
        <p className="text-muted-foreground">
          This order is for someone whose access plans include every step. A request skips the steps that the
          person&apos;s or the app&apos;s access plan leaves out.
          {fallback ? " If a paid key ends up first for someone, it is used directly, not as a fallback." : ""}
        </p>
      )}
      {pins.size > 0 && (
        <p className="text-muted-foreground">Use the pinned name to send requests only through this subscription, never through another connection or paid key.</p>
      )}
    </li>
  );
}
