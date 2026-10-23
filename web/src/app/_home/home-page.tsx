"use client";

import Link from "next/link";
import { useEffect, useReducer, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useViewer } from "@/components/shell/session";
import { api, queryKeys } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatAppName } from "@/lib/format";
import { activeCapRefusal, CAP_WARNING } from "@/lib/capability-state";
import { useCapabilityViews } from "@/lib/use-capability-views";
import { useOptionalProviderStatus } from "@/lib/provider-status";
import { ownerEngineAlert, ownerProviderAlert } from "@/components/shell/use-owner-alert";
import { appPlumbingCapabilities } from "@/lib/terms";
import { Button } from "@/components/ui/button";
import { AppMark } from "@/components/product/app-mark";
import { CapNotice } from "@/components/product/cap-notice";
import { ErrorState } from "@/components/product/error-state";
import { PageHeader } from "@/components/product/page-header";
import { RelativeTime } from "@/components/product/relative-time";
import { capabilityMeta } from "@/components/product/readiness";
import { homeApi, type HomeCapability, type HomeSnapshot } from "./home-api";
import { HomeComposer } from "./home-composer";
import { Recent } from "./home-recent";
import { OwnerPanel } from "./home-owner-panel";
import {
  capabilityLabel,
  chooserRows,
  firstNameOf,
  gatewayNeeds,
  greetingFor,
  kindOf,
  listFacts,
  orderChooser,
  recentItems,
  workingSentence,
} from "./home-model";

const homeKey = ["account", "home"];
/** Recent's media: the same query whether Home or the early fetch asks first. */
const recentMediaQuery = {
  queryKey: ["library", "home-recent-creations"],
  queryFn: homeApi.creations,
  staleTime: 30_000,
};
const OWNER_ROLES = ["owner", "superadmin", "admin"];
const DEFAULT_VIEWER_NAME = "Your account";

/** The time as a number of seconds, re-read on each render; the page re-renders while work is in progress. */
function useTicking(active: boolean) {
  const [, tick] = useReducer((count: number) => count + 1, 0);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(tick, 15_000);
    return () => window.clearInterval(id);
  }, [active]);
}

function lastUsed(app: HomeSnapshot["apps"][number]): number | null {
  const times = app.credentials
    .map((key) => key.last_used_at)
    .filter((value): value is number => typeof value === "number");
  return times.length ? Math.max(...times) : null;
}

/** The two apps used most recently, and "Connect an app" (the one way to add another). */
function YourApps({ home }: { home: HomeSnapshot }) {
  const apps = home.apps
    .filter((app) => !app.disabled_at)
    .toSorted((a, b) => (lastUsed(b) ?? 0) - (lastUsed(a) ?? 0));
  return (
    <section aria-label="In your apps" className="border-t pt-4 flex flex-wrap items-center gap-x-5 gap-y-2">
      <p className="text-muted-foreground text-meta">In your apps</p>
      {apps.slice(0, 2).map((app) => {
        const name = formatAppName(app.name, home.user.email);
        const used = lastUsed(app);
        return (
          <Link
            key={app.id}
            prefetch={false}
            href={`/apps/?app=${encodeURIComponent(app.id)}`}
            className="text-body group inline-flex min-h-11 min-w-0 items-center gap-2 font-medium"
          >
            <AppMark app={name} className="size-5" />
            <span className="truncate group-hover:underline">{name}</span>
            {used ? (
              <span className="text-muted-foreground text-meta font-normal">
                <RelativeTime ts={used} fallback="" />
              </span>
            ) : null}
          </Link>
        );
      })}
      {!apps.length ? <span className="text-muted-foreground text-meta">No apps yet.</span> : null}
      <Link
        prefetch={false}
        href="/connect/"
        className="text-link text-meta ml-auto inline-flex min-h-11 items-center gap-0.5 font-medium underline-offset-4 hover:underline"
      >
        Connect an app
        <ChevronRight aria-hidden="true" className="size-4" />
      </Link>
    </section>
  );
}

/** Nothing to show yet: what the person can make, each row choosing that kind and focusing the box. */
function FirstVisitList({
  rows,
  onChoose,
}: {
  rows: HomeCapability[];
  onChoose: (kind: string) => void;
}) {
  return (
    <section aria-labelledby="make-here-heading" className="space-y-3">
      <h2 id="make-here-heading" className="text-heading">
        What you can make here
      </h2>
      <ul className="rounded-control grid border sm:grid-cols-3">
        {rows.map((row) => {
          const Icon = capabilityMeta(kindOf(row)).icon;
          return (
            <li key={kindOf(row)} className="border-b last:border-b-0 sm:border-r sm:[&:nth-child(3n)]:border-r-0">
              <button
                type="button"
                onClick={() => {
                  onChoose(kindOf(row));
                  window.requestAnimationFrame(() => document.getElementById("home-prompt")?.focus());
                }}
                className="hover:bg-muted/50 flex min-h-11 w-full flex-col gap-1 p-4 text-left"
              >
                <span className="flex items-center gap-2 font-medium">
                  <Icon aria-hidden="true" className="text-foreground size-4" />
                  {capabilityLabel(row)}
                </span>
                {row.summary ? <span className="text-muted-foreground text-meta">{row.summary}</span> : null}
                <span className="text-muted-foreground text-badge">{listFacts(row)}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** A member with no access plan can make nothing; an owner is told to choose one. */
function NoPlanNotice({ owner, ownerName }: { owner: boolean; ownerName: string | null }) {
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const who = ownerName ?? "the gateway owner";
  if (owner)
    return (
      <div className="bg-card rounded-control flex flex-wrap items-center justify-between gap-3 border px-4 py-3">
        <p className="text-meta">
          <span className="font-semibold">You can’t make anything yet.</span> Choose an access plan for yourself.
        </p>
        <Button asChild variant="outline" size="sm">
          <Link prefetch={false} href="/people?tab=plans">
            Choose an access plan
          </Link>
        </Button>
      </div>
    );
  return (
    <div className="bg-card rounded-control flex flex-wrap items-center justify-between gap-3 border px-4 py-3">
      <p className="text-meta">
        <span className="font-semibold">You can’t make anything yet.</span> {who} needs to choose an access plan for
        you.
      </p>
      {state === "sent" ? (
        <p role="status" className="text-meta text-muted-foreground">
          Sent to {who}.
        </p>
      ) : (
        <div className="flex items-center gap-3">
          {state === "failed" ? (
            <p role="alert" className="text-destructive text-meta">
              Couldn’t send that. Try again.
            </p>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            disabled={state === "sending"}
            onClick={async () => {
              setState("sending");
              try {
                await homeApi.requestAccess();
                setState("sent");
              } catch {
                setState("failed");
              }
            }}
          >
            Ask {who}
          </Button>
        </div>
      )}
    </div>
  );
}

export default function HomePage() {
  const viewer = useViewer();
  const {
    data: home,
    isPending: homePending,
    isError: homeError,
    refetch: refetchHome,
    isFetching: homeFetching,
  } = useQuery({ queryKey: homeKey, queryFn: homeApi.home });
  const signedIn = Boolean(home?.authenticated);
  const isOwner = Boolean(
    home?.authenticated && OWNER_ROLES.includes(home.user.role) && viewer.admin,
  );
  const model = useCapabilityViews(isOwner);
  const media = useQuery({ ...recentMediaQuery, enabled: signedIn });
  const [kind, setKind] = useState<string | null>(null);

  // Owner facts come from the same cached reads the shell's owner alert uses, so they add no request.
  const status = useQuery({ queryKey: queryKeys.status, queryFn: api.status, enabled: isOwner });
  const registry = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    enabled: isOwner,
  }).data;
  const providers = useOptionalProviderStatus(isOwner);

  const rows = home?.authenticated ? orderChooser(chooserRows(home.capabilities), home.receipts ?? []) : [];
  const items = home?.authenticated
    ? recentItems({
        receipts: home.receipts ?? [],
        media: media.data?.assets ?? [],
        chats: home.recent_chats ?? [],
        rows: home.capabilities,
      })
    : [];
  const mediaState = media.isError ? "error" : media.isPending ? "pending" : "ready";
  const firstVisit = signedIn && mediaState === "ready" && items.length === 0;
  const refusal = activeCapRefusal(model.requests);
  const ownerName = home?.authenticated ? (home.gateway?.owner_name?.trim() || null) : null;
  const hasMaking = items.some((item) => item.kind === "making");
  useTicking(hasMaking);
  const now = Date.now() / 1000;
  const viewerName = viewer.name && viewer.name !== DEFAULT_VIEWER_NAME ? viewer.name : null;
  const firstName = firstNameOf(home?.authenticated ? home.user.name : viewerName);
  const greeting = greetingFor(new Date(), firstName, firstVisit);
  const engineFault = isOwner && !status.isError ? ownerEngineAlert(status.data?.current_health, registry) : null;
  const providerFault =
    isOwner && providers.state === "ready" ? ownerProviderAlert(providers.providers) : null;
  const noEngine =
    isOwner &&
    registry != null &&
    !Object.values(registry.services).some((service) => service.source !== "builtin" && service.enabled);
  const needs = gatewayNeeds({ engineFault, providerFault, noEngine });
  const working = workingSentence(model.views.filter((view) => !appPlumbingCapabilities.has(view.key)));
  const warning = refusal ? (refusal.warnings ?? []).find((text) => CAP_WARNING.test(text)) ?? "" : null;

  if (homeError) {
    return (
      <>
        <PageHeader title="Home" />
        <ErrorState
          heading="The gateway didn’t answer."
          message="Your work is safe. Try again."
          onRetry={() => void refetchHome()}
          retrying={homeFetching}
        />
      </>
    );
  }
  if (home && !home.authenticated) {
    return (
      <>
        <PageHeader title="Home" />
        <ErrorState
          severity="info"
          heading="Sign in to use your AI"
          message="Make pictures, chat and more with the apps you already use."
          action={{
            label: "Sign in",
            href: `/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/" : window.location.pathname + window.location.search)}`,
          }}
        />
      </>
    );
  }

  // One tree while the home read is pending and after it settles, so what was typed stays in the box.
  const composer = homePending ? (
    <HomeComposer
      rows={[]}
      views={[]}
      loading
      owner={false}
      ownerName={null}
      paidOff={false}
      kind={null}
      onKindChange={() => undefined}
    />
  ) : home?.has_access_plan === false ? (
    <NoPlanNotice owner={isOwner} ownerName={ownerName} />
  ) : rows.length ? (
    <HomeComposer
      rows={rows}
      views={model.views}
      loading={false}
      owner={isOwner}
      ownerName={ownerName}
      paidOff={refusal != null}
      kind={kind}
      onKindChange={setKind}
    />
  ) : (
    <p className="text-meta text-muted-foreground">
      {isOwner ? "Nothing is available yet." : `Nothing is available yet. Ask ${ownerName ?? "the gateway owner"} to turn something on.`}
    </p>
  );

  return (
    <div className="min-w-0 space-y-8">
      <header className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight">{greeting}</h1>
        {firstVisit && !isOwner && ownerName ? (
          <p className="text-muted-foreground text-body">{ownerName} set up this gateway for the household.</p>
        ) : null}
      </header>
      {refusal ? <CapNotice warning={warning ?? ""} at={refusal.at} owner={isOwner} /> : null}
      {composer}
      <div
        className={cn(
          "space-y-8",
          isOwner && "lg:grid lg:grid-cols-[minmax(0,1fr)_340px] lg:items-start lg:gap-x-8 lg:space-y-0",
        )}
      >
        {isOwner ? (
          <div className="min-w-0 lg:col-start-2 lg:row-start-1">
            <OwnerPanel needs={needs} working={working} checkedAt={model.verdict.checkedAt ?? null} />
          </div>
        ) : null}
        <div className="min-w-0 space-y-8 lg:col-start-1 lg:row-start-1">
          {firstVisit ? (
            rows.length ? (
              <FirstVisitList rows={rows} onChoose={setKind} />
            ) : null
          ) : (
            <Recent
              items={items}
              now={now}
              views={model.views}
              owner={isOwner}
              mediaState={mediaState}
              onRetry={() => void media.refetch()}
              retrying={media.isFetching}
            />
          )}
        </div>
        {home?.authenticated ? (
          <div className="lg:col-span-2 lg:row-start-2">
            <YourApps home={home} />
          </div>
        ) : null}
      </div>
    </div>
  );
}
