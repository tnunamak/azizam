"use client";

import Link from "next/link";
import { useEffect, useReducer, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Pause, Play, Video } from "lucide-react";
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
import { MediaBadge, Waveform } from "@/components/product/audio-cover";
import { BrandMark } from "@/components/product/brand-mark";
import { CapNotice } from "@/components/product/cap-notice";
import { ErrorState } from "@/components/product/error-state";
import { PageHeader } from "@/components/product/page-header";
import { RelativeTime } from "@/components/product/relative-time";
import { capabilityMeta, SectionHeading } from "@/components/product/readiness";
import { homeApi, type HomeCapability, type HomeSnapshot } from "./home-api";
import { HomeComposer, selectedRow } from "./home-composer";
import { RequestHistoryNotice } from "./request-history-notice";
import { Recent } from "./home-recent";
import { OwnerPanel } from "./home-owner-panel";
import {
  capabilityLabel,
  chooserRows,
  firstNameOf,
  gatewayNeeds,
  greetingParts,
  kindOf,
  listFacts,
  offeredRows,
  orderChooser,
  recentItems,
  workingLine,
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

/** The two apps used most recently, and "Connect an app" (the one way to add another), in Recent's section grammar. */
function YourApps({ home }: { home: HomeSnapshot }) {
  const apps = home.apps
    .filter((app) => !app.disabled_at)
    .toSorted((a, b) => (lastUsed(b) ?? 0) - (lastUsed(a) ?? 0));
  return (
    <section aria-labelledby="apps-heading">
      <SectionHeading
        id="apps-heading"
        title="Your apps"
        action={
          <Link
            prefetch={false}
            href="/connect/"
            className="text-link text-meta inline-flex items-center gap-0.5 font-medium underline-offset-4 hover:underline max-sm:min-h-11"
          >
            Connect an app
            <ChevronRight aria-hidden="true" className="size-4" />
          </Link>
        }
      />
      {apps.length ? (
        // One app per line at 480 px and below, so three apps never wrap two and one.
        <ul className="flex flex-wrap gap-x-8 gap-y-2 max-[30rem]:flex-col">
          {apps.slice(0, 3).map((app) => {
            const name = formatAppName(app.name, home.user.email);
            const used = lastUsed(app);
            return (
              <li key={app.id} className="min-w-0">
                <Link
                  prefetch={false}
                  href={`/apps/?app=${encodeURIComponent(app.id)}`}
                  className="group flex min-w-0 items-center gap-3 max-sm:min-h-11"
                >
                  <AppMark app={name} className="size-9" />
                  <span className="min-w-0">
                    <span className="text-meta block truncate font-medium group-hover:underline">{name}</span>
                    <span className="text-muted-foreground text-badge block">
                      {used ? (
                        <>
                          Used <RelativeTime ts={used} fallback="" />
                        </>
                      ) : (
                        "Not used yet"
                      )}
                    </span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-muted-foreground text-meta">No apps yet.</p>
      )}
    </section>
  );
}

/**
 * Cards keep about the size of Recent's tiles whatever their count: up to four share a row of four from
 * 640 px (one card stays one tile, never a banner across the column), and five or six fill a row of their own
 * from 1024 px. On a phone they sit two by two.
 */
const STARTER_COLUMNS = [
  "grid-cols-2 sm:grid-cols-4",
  "grid-cols-2 sm:grid-cols-4",
  "grid-cols-2 sm:grid-cols-4",
  "grid-cols-2 sm:grid-cols-4",
  "grid-cols-2 sm:grid-cols-4",
  "grid-cols-2 sm:grid-cols-3 lg:grid-cols-5",
  "grid-cols-2 sm:grid-cols-3 lg:grid-cols-6",
];

/** A first visit's sample chat reply: the shape of what Chat gives back. */
const SAMPLE_REPLY = "Day one: take the 9:10 coastal train to Seacliff, then ride the shore path to the meadow for lunch.";

/**
 * Bars for a sample waveform, the same on every render. Speech comes in word-sized bursts with short
 * pauses between them; anything else (music, sound) is one continuous swell.
 */
function samplePeaks(kind: string): number[] {
  const speech = kind === "speech" || kind === "voice";
  return Array.from({ length: 36 }, (_, index) => {
    if (speech) {
      const inWord = index % 7;
      if (inWord >= 5) return 0.08;
      return 0.35 + 0.6 * Math.abs(Math.sin(index * 1.7));
    }
    const swell = Math.sin((Math.PI * index) / 35);
    return 0.2 + 0.75 * swell * (0.7 + 0.3 * Math.abs(Math.sin(index * 0.9)));
  });
}

/**
 * Each medium's own soft two-stop well, from the capability hues: speech teal, music amber to rose (rose
 * leads in dark, where amber over the deep-water page turns olive). Dark wells are a 22% tint on the surface.
 */
function audioWell(kind: string): string {
  if (kind === "speech" || kind === "voice")
    return "bg-linear-to-br from-capability-chat/22 to-capability-chat/8 dark:from-capability-chat/22 dark:to-capability-chat/10";
  return "bg-linear-to-br from-capability-voice/22 to-capability-video/18 dark:from-capability-video/22 dark:to-capability-voice/12";
}

/** The sample a card's play disc plays: only speech has one the web app ships (home-sample-voice.mp3). */
function sampleAudio(row: HomeCapability): string | null {
  const kind = kindOf(row);
  return (row.output_modalities ?? []).includes("audio") && (kind === "speech" || kind === "voice")
    ? "/home-sample-voice.mp3"
    : null;
}

/**
 * What a kind of work gives back, by the modality the row says it makes: a picture, a clip's poster, a
 * waveform in the medium's own well, or a reply. The pictures and the voice clip are samples the web app
 * ships (home-sample-*), not anyone's work.
 */
function SampleFace({ row }: { row: HomeCapability }) {
  const outputs = row.output_modalities ?? [];
  const { icon: Icon, tone, tint } = capabilityMeta(kindOf(row));
  if (outputs.includes("image") || outputs.includes("video")) {
    const video = !outputs.includes("image");
    return (
      <>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={video ? "/home-sample-clip.webp" : "/home-sample-picture.webp"}
          alt=""
          loading="lazy"
          className="size-full object-cover"
        />
        {video ? <MediaBadge icon={Video} className="absolute bottom-2 left-2" /> : null}
      </>
    );
  }
  if (outputs.includes("audio"))
    return (
      <span className={cn("text-foreground/60 flex size-full items-center justify-center", audioWell(kindOf(row)))}>
        <Waveform peaks={samplePeaks(kindOf(row))} className="h-1/3 w-3/4 flex-none" />
      </span>
    );
  if (outputs.includes("text"))
    return (
      <span className="bg-muted flex size-full flex-col p-4">
        <Icon aria-hidden="true" className={cn("size-4 shrink-0", tone)} />
        <span className="text-foreground text-label mt-3 line-clamp-3 font-normal">{SAMPLE_REPLY}</span>
      </span>
    );
  return (
    <span className={cn("flex size-full items-center justify-center", tint)}>
      <Icon aria-hidden="true" className={cn("size-8", tone)} />
    </span>
  );
}

/** The sample disc: 36 px, white with a dark glyph, as on Recent's dark audio tile. It plays the sample. */
const SAMPLE_DISC =
  "pointer-events-auto flex size-9 items-center justify-center bg-sample-disc text-sample-disc-foreground rounded-full shadow-sm transition-opacity hover:opacity-90 focus-visible:ring-ring focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none [&_svg]:size-4 [&_svg]:translate-x-px [&_svg]:fill-current max-sm:size-11";

/**
 * Nothing to show yet, so the chooser is a row of cards under the composer, on the composer's 760 px edges:
 * each card's face is what that kind of work gives back, then its name and a facts slot of two lines, so
 * every caption ends on one baseline. Choosing one chooses that kind and focuses the box. The composer shows
 * no chips meanwhile: these cards are the chooser, not a repeat of it. A face that has a sample to hear has
 * its own play button, beside the card's button rather than inside it.
 */
function FirstVisitList({
  rows,
  chosen,
  onChoose,
}: {
  rows: HomeCapability[];
  chosen: string | null;
  onChoose: (kind: string) => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  return (
    <section aria-labelledby="make-here-heading">
      <h2 id="make-here-heading" className="sr-only">
        What you can make here
      </h2>
      <audio ref={audioRef} preload="none" onEnded={() => setPlaying(null)} onPause={() => setPlaying(null)} className="sr-only" />
      <div
        role="group"
        aria-label="What to make"
        className={cn("grid gap-x-4 gap-y-6", STARTER_COLUMNS[Math.min(rows.length, STARTER_COLUMNS.length - 1)])}
      >
        {rows.map((row) => {
          const kind = kindOf(row);
          const { icon: Icon, tone } = capabilityMeta(kind);
          const facts = listFacts(row);
          const sample = sampleAudio(row);
          return (
            <div key={kind} className="relative min-w-0">
              <button
                type="button"
                aria-pressed={chosen === kind}
                onClick={() => {
                  onChoose(kind);
                  window.requestAnimationFrame(() => document.getElementById("home-prompt")?.focus());
                }}
                className="group block w-full min-w-0 text-left"
              >
                <span className="bg-muted rounded-media relative block aspect-4/3 overflow-hidden">
                  <SampleFace row={row} />
                  {/* The chosen ring is drawn inside the face, so it never hangs outside the grid. */}
                  <span
                    aria-hidden="true"
                    className="rounded-media group-hover:shadow-[inset_0_0_0_2px_var(--color-border)] group-aria-pressed:shadow-[inset_0_0_0_2px_var(--color-ring)] pointer-events-none absolute inset-0 transition-shadow duration-(--motion-fast)"
                  />
                </span>
                <span className="text-foreground text-meta mt-3 flex items-center gap-1.5 font-medium">
                  <Icon aria-hidden="true" className={cn("size-4 shrink-0", tone)} />
                  {capabilityLabel(row)}
                </span>
                <span className="text-muted-foreground text-badge mt-0.5 line-clamp-2 min-h-8">{facts}</span>
              </button>
              {sample ? (
                <span className="pointer-events-none absolute inset-x-0 top-0 flex aspect-4/3 items-center justify-center">
                  <button
                    type="button"
                    className={SAMPLE_DISC}
                    aria-label={playing === kind ? `Pause the ${capabilityLabel(row)} sample` : `Play a ${capabilityLabel(row)} sample`}
                    onClick={async () => {
                      const audio = audioRef.current;
                      if (!audio) return;
                      if (playing === kind) {
                        audio.pause();
                        return;
                      }
                      audio.src = sample;
                      try {
                        await audio.play();
                        setPlaying(kind);
                      } catch {
                        setPlaying(null);
                      }
                    }}
                  >
                    {playing === kind ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
                  </button>
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
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

  const rows = home?.authenticated
    ? offeredRows(orderChooser(chooserRows(home.capabilities), home.receipts ?? []), model.views)
    : [];
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
  const greeting = greetingParts(new Date(), firstName, firstVisit);
  const engineFault = isOwner && !status.isError ? ownerEngineAlert(status.data?.current_health, registry) : null;
  const providerFault =
    isOwner && providers.state === "ready" ? ownerProviderAlert(providers.providers) : null;
  const noEngine =
    isOwner &&
    registry != null &&
    !Object.values(registry.services).some((service) => service.source !== "builtin" && service.enabled);
  const ownerViews = model.views.filter((view) => !appPlumbingCapabilities.has(view.key));
  const needs = gatewayNeeds({ engineFault, providerFault, noEngine, capabilities: ownerViews });
  const chosenRow = selectedRow(rows, model.views, kind);
  // A first visit's cards are the chooser when there is a choice to make; one mode stays a single chip.
  const cardsChoose = firstVisit && rows.length > 1;
  const lead = firstVisit && !isOwner && ownerName ? `${ownerName} set up this gateway for the household.` : null;
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
      chooser={!cardsChoose}
    />
  ) : (
    <p className="text-meta text-muted-foreground text-center">
      {isOwner ? "Nothing is available yet." : `Nothing is available yet. Ask ${ownerName ?? "the gateway owner"} to turn something on.`}
    </p>
  );

  return (
    // One centred column: every section shares its two edges, and the composer sits centred inside it.
    <div className="mx-auto w-full max-w-(--content-home) min-w-0">
      <section aria-labelledby="home-greeting" className="sm:pt-6 lg:pt-10">
        <header className="flex items-center justify-center gap-3">
          <BrandMark className="size-7 max-sm:hidden" />
          <h1 id="home-greeting" className="text-greeting text-center text-balance max-sm:text-2xl">
            {greeting.lead}
            {greeting.name ? (
              <>
                , <span className="text-muted-foreground">{greeting.name}</span>
              </>
            ) : null}
          </h1>
        </header>
        <div className="mt-8 space-y-3 sm:mt-10">
          {refusal ? <CapNotice warning={warning ?? ""} at={refusal.at} owner={isOwner} /> : null}
          {composer}
        </div>
      </section>
      {/* A one-time notice (r103) sits between the hero and the person's work, on the column's edges. */}
      <div className="mt-10 empty:hidden">
        <RequestHistoryNotice />
      </div>
      <div
        className={cn(
          "min-w-0 space-y-10",
          // The first visit's cards are the chooser, so they sit close under the composer; Recent sits apart.
          firstVisit ? "mt-8 sm:mt-10" : "mt-14 sm:mt-18 lg:mt-20",
          // A member's first visit keeps the composer's 760 px edges all the way down: one set of edges.
          firstVisit && !isOwner && "mx-auto max-w-(--content-composer)",
          isOwner && "lg:grid lg:grid-cols-[minmax(0,1fr)_18.75rem] lg:items-start lg:gap-x-8 lg:space-y-0",
        )}
      >
        {isOwner ? (
          <div className="min-w-0 lg:col-start-2 lg:row-start-1">
            <OwnerPanel needs={needs} working={workingLine(ownerViews)} />
          </div>
        ) : null}
        <div className="min-w-0 space-y-10 lg:col-start-1 lg:row-start-1">
          {firstVisit ? (
            <div className="space-y-4">
              {/* Who set this up: a quiet centred line over the row, not a section intro. */}
              {lead ? <p className="text-muted-foreground text-label text-center font-normal">{lead}</p> : null}
              {cardsChoose ? <FirstVisitList rows={rows} chosen={chosenRow ? kindOf(chosenRow) : null} onChoose={setKind} /> : null}
            </div>
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
          {home?.authenticated ? <YourApps home={home} /> : null}
        </div>
      </div>
    </div>
  );
}
