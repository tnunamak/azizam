"use client";

import { JobNotices } from "@/components/jobs/job-notices";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { ThemeProvider, useTheme } from "next-themes";
import {
  Activity,
  Box,
  ChartColumn,
  ChevronDown,
  ChevronRight,
  Cloud,
  Cpu,
  GitBranch,
  House,
  Images,
  KeyRound,
  Menu,
  Moon,
  Play,
  ReceiptText,
  Tags,
  CornerDownRight,
  Settings,
  Sun,
  Users,
  type LucideIcon,
} from "lucide-react";
import { createQueryClient } from "@/lib/api";
import {
  canonicalDestinationPath,
  destinations,
  pageAccess,
  pageNameFor,
  PRODUCT_NAME,
  uiPages,
} from "@/lib/product";
import { useCapabilityViews } from "@/lib/use-capability-views";
import {
  STATE_WORDS,
  type CapabilityView,
  type StateTone,
} from "@/lib/capability-state";
import { useOwnerAlertTarget } from "./use-owner-alert";
import { ServiceNotice } from "@/components/product/service-notice";
import { Button } from "@/components/ui/button";
import { BrandMark } from "@/components/product/brand-mark";
import { PersonMark } from "@/components/product/person-mark";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ToastGate } from "./toast-gate";
import { ErrorState } from "@/components/product/error-state";
import { PageHeader } from "@/components/product/page-header";
import { RelativeTime } from "@/components/product/relative-time";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ViewerContext, useViewer, useViewerQuery } from "./session";
// Keep the shortcut listener in the shell; load the palette and account menu
// outside the first paint, then fetch them once the page is idle so the first
// open does not wait on a network round trip.
const loadCommandPalette = () => import("@/components/ui/command");
const loadAccountMenu = () => import("./account-menu-content");
const CommandPalette = lazy(() =>
  loadCommandPalette().then((module) => ({ default: module.CommandPalette })),
);
const AccountMenuContent = lazy(() =>
  loadAccountMenu().then((module) => ({ default: module.AccountMenuContent })),
);

function usePreloadShellExtras() {
  useEffect(() => {
    const preload = () => {
      void loadCommandPalette();
      void loadAccountMenu();
    };
    if (typeof window.requestIdleCallback === "function") {
      const handle = window.requestIdleCallback(preload, { timeout: 2000 });
      return () => window.cancelIdleCallback(handle);
    }
    const timer = window.setTimeout(preload, 1000);
    return () => window.clearTimeout(timer);
  }, []);
}

// One icon per destination; no two destinations share a glyph (ban-list 19).
const icons: Record<string, LucideIcon> = {
  Home: House,
  Library: Images,
  Try: Play,
  "Apps & keys": KeyRound,
  Models: Box,
  Workflows: GitBranch,
  Usage: ChartColumn,
  Status: Activity,
  Engines: Cpu,
  Providers: Cloud,
  People: Users,
  Categories: Tags,
  Activity: ReceiptText,
  Settings,
};
const queryClient = createQueryClient();

function signInReturnPath(pathname: string, search: string): string {
  if (typeof window !== "undefined")
    return `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const normalizedPath = pathname.startsWith("/") ? pathname : `/${pathname}`;
  return `${normalizedPath}${search ? `?${search}` : ""}`;
}

function signInHref(returnPath: string): string {
  return `/sign-in/?return_to=${encodeURIComponent(returnPath)}`;
}

function ThemeToggle({ chrome = false }: { chrome?: boolean }) {
  const { resolvedTheme, setTheme } = useTheme();
  const toggle = () => setTheme(resolvedTheme === "dark" ? "light" : "dark");
  const icons = (
    <>
      <Sun className="size-4 dark:hidden" aria-hidden="true" />
      <Moon className="hidden size-4 dark:block" aria-hidden="true" />
    </>
  );
  return chrome ? (
    <button
      type="button"
      aria-label="Toggle theme"
      className="shell-nav-footer size-11 shrink-0 justify-center px-0 lg:size-9"
      onClick={toggle}
    >
      {icons}
    </button>
  ) : (
    <Button
      variant="ghost"
      size="icon"
      className="min-h-11 min-w-11"
      aria-label="Toggle theme"
      onClick={toggle}
    >
      {icons}
    </Button>
  );
}

function AccountMenu({ compact = false }: { compact?: boolean }) {
  const viewer = useViewer();
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const openMenu = () => {
    setLoaded(true);
    setOpen(true);
  };

  return (
    <>
      {loaded ? (
        <Suspense
          fallback={
            <>
              <AccountMenuPlaceholder
                compact={compact}
                name={viewer.name}
                expanded={open}
                onClick={openMenu}
              />
              {open && <p role="status">Loading navigation…</p>}
            </>
          }
        >
          <AccountMenuContent
            compact={compact}
            open={open}
            onOpenChange={setOpen}
          />
        </Suspense>
      ) : (
        <AccountMenuPlaceholder
          compact={compact}
          name={viewer.name}
          onClick={openMenu}
          onFocus={() => void import("./account-menu-content")}
          onPointerEnter={() => void import("./account-menu-content")}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              openMenu();
            }
          }}
        />
      )}
    </>
  );
}

function AccountMenuPlaceholder({
  compact,
  name,
  expanded = false,
  onClick,
  ...props
}: {
  compact: boolean;
  name: string;
  expanded?: boolean;
  onClick: () => void;
  onFocus?: () => void;
  onPointerEnter?: () => void;
  onKeyDown?: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
}) {
  return (
    <button
      type="button"
      className={
        compact
          ? "rounded-control flex size-11 items-center justify-center"
          : "shell-nav-footer w-full justify-center px-2 lg:justify-start lg:px-3"
      }
      aria-label={`Account menu: ${name}`}
      aria-haspopup="menu"
      aria-expanded={expanded}
      onClick={onClick}
      {...props}
    >
      <PersonMark name={name} size={compact ? "md" : "sm"} />
      {!compact && (
        <span className="shell-nav-label text-chrome-foreground min-w-0 flex-1 truncate text-left font-medium">
          {name}
        </span>
      )}
      {!compact && <ChevronDown className="shell-nav-label size-4" />}
    </button>
  );
}

/** Not-Ready capabilities split into faults, unchecked (Unknown) and Busy. */
function sortProblems(problems: CapabilityView[]) {
  return {
    faults: problems.filter(
      (view) => view.state !== "busy" && view.state !== "unknown",
    ),
    unchecked: problems.filter((view) => view.state === "unknown"),
    busy: problems.filter((view) => view.state === "busy"),
  };
}

/**
 * The count and colour for the capabilities that are not Ready (Status model
 * rule 3). Busy is not a fault and is not counted. A fault (Down, Blocked,
 * Degraded) is an "issue" in its own colour; the worst fault sets the colour.
 * Capabilities with no fresh check are "unchecked", never an "issue", because
 * Unknown is missing knowledge, not a known fault. Faults outrank Unknown, so
 * a mix counts only the faults.
 */
export function problemPill(
  problems: CapabilityView[],
): { label: string; tone: StateTone } | null {
  const { faults, unchecked } = sortProblems(problems);
  if (faults.length) {
    const worst = faults.reduce((a, b) =>
      STATE_WORDS[b.state].rank > STATE_WORDS[a.state].rank ? b : a,
    );
    return {
      label: `${faults.length} issue${faults.length === 1 ? "" : "s"}`,
      tone: worst.tone,
    };
  }
  if (unchecked.length) return { label: "Not checked yet", tone: "unknown" };
  return null;
}

/**
 * State fills, the same as strips and legends (D1.5). Unknown is a ring, the
 * "grey dashed" of the state palette: at this size a dash is not legible.
 */
function dotClass(tone: StateTone): string {
  switch (tone) {
    case "ok":
      return "bg-success-fill";
    case "warn":
      return "bg-warning-fill";
    case "bad":
      return "bg-danger-fill";
    case "busy":
      return "bg-info-fill";
    case "blocked":
      return "bg-blocked-fill";
    case "unknown":
      return "border-[1.5px] border-neutral bg-transparent";
    default:
      return "bg-neutral";
  }
}

function HealthLink({ compact = false }: { compact?: boolean }) {
  const viewer = useViewer();
  const {
    data: gatewayData,
    isError: gatewayError,
    dataUpdatedAt: gatewayCheckedAt,
  } = useQuery({
    queryKey: ["foundation", "reachability"],
    queryFn: async () => {
      const r = await fetch("/health");
      if (!r.ok) throw new Error("Gateway is unreachable");
      return r.json();
    },
    refetchInterval: 60000,
  });
  // The same capability model as Home and Status: the dot cannot say
  // "fine" while something the viewer depends on is failing.
  const model = useCapabilityViews(viewer.admin && viewer.authenticated);
  // Owners also see the engine or provider at fault, in the banner's words.
  const ownerAlert = useOwnerAlertTarget(viewer.admin);
  const signedIn = viewer.authenticated && !viewer.serviceAdmin;
  // One rule (problemPill): faults are issues; Unknown (no fresh check) is
  // listed after them in the popover but never counted; Busy is neither.
  const sorted = sortProblems(model.verdict.problems);
  const listedProblems = [...sorted.faults, ...sorted.unchecked];
  const busy = sorted.busy.length;
  const pill = problemPill(model.verdict.problems);
  const statusError = model.isError || model.ownerHealthError;
  const currentCapabilities = !!gatewayData && !gatewayError && !model.isPending && !statusError;
  const { label, tone } = gatewayError
    ? { label: "Gateway unreachable", tone: "bad" as const }
    : !gatewayData
      ? { label: "Checking", tone: "unknown" as const }
      : !signedIn
        ? { label: "Gateway reachable", tone: "ok" as const }
        : !currentCapabilities
          ? { label: statusError ? "Status unknown" : "Checking", tone: "unknown" as const }
            : pill
              ? pill
              : busy
                ? { label: "Busy", tone: "busy" as const }
                : model.verdict.tone === "off"
                  ? { label: "Nothing turned on", tone: "off" as const }
                  : { label: "All working", tone: "ok" as const };
  const visibleLabel = label;
  const dot = dotClass(tone);
  // The capabilities behind the count: tapping the pill must explain it.
  const listed = signedIn && currentCapabilities;
  const reasons = listed ? listedProblems : [];
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={
            compact
              ? "text-chrome-foreground hover:bg-chrome-raised rounded-control flex min-h-11 min-w-11 items-center justify-center gap-2 px-2.5 text-sm"
              : "shell-nav-footer"
          }
          aria-label={`Gateway status: ${visibleLabel}`}
        >
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${dot}`}
          />
          <span className={compact ? "text-xs" : "shell-nav-label"}>
            {visibleLabel}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={compact ? "end" : "start"}
        side={compact ? "bottom" : "top"}
        className="w-80 max-w-[calc(100vw-2rem)] space-y-3"
        aria-label="Gateway health"
      >
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Gateway</dt>
          <dd>
            {gatewayError ? (
              "Not reachable"
            ) : gatewayData ? (
              <>
                Reachable, checked{" "}
                <RelativeTime ts={gatewayCheckedAt || null} fallback="now" />
              </>
            ) : (
              "Checking"
            )}
          </dd>
          {/* With the gateway unreachable, nothing current can be said about it. */}
          {listed ? (
            <>
              <dt className="text-muted-foreground">Your AI</dt>
              <dd>
                {model.verdict.text}
                {reasons.length ? (
                  <ul className="mt-1.5 space-y-1.5">
                    {reasons.map((view) => (
                      <li key={view.key}>
                        <span className="flex items-center gap-2 font-medium">
                          <span
                            aria-hidden="true"
                            className={`size-2 shrink-0 rounded-full ${dotClass(view.tone)}`}
                          />
                          {view.label} · {view.word}
                        </span>
                        {view.detail ? (
                          <span className="text-muted-foreground block pl-4 text-xs">
                            {view.detail}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </dd>
            </>
          ) : null}
          {listed && ownerAlert ? (
            <>
              <dt className="text-muted-foreground">{ownerAlert.section}</dt>
              <dd>{ownerAlert.message}</dd>
            </>
          ) : null}
        </dl>
        <Button variant="outline" size="sm" asChild>
          <Link href="/status">
            {reasons.length ? "See why" : "View status"}
            <ChevronRight aria-hidden="true" />
          </Link>
        </Button>
      </PopoverContent>
    </Popover>
  );
}

function Nav({
  expanded = false,
  onNavigate,
}: {
  expanded?: boolean;
  onNavigate?: () => void;
}) {
  const viewer = useViewer();
  const pathname = usePathname();
  return (
    <nav
      aria-label="Main navigation"
      className={`flex flex-col gap-5 ${expanded ? "sheet-nav" : ""}`}
    >
      {["You", ...(viewer.admin ? ["Gateway"] : [])].map((group) => (
        // The owner's tools sit quieter than the household's own places
        // (D1.5: Engines and Providers weighed as much as Home and Library).
        <div
          key={group}
          className={group === "Gateway" ? "shell-group-admin" : undefined}
        >
          <p className="shell-group-label">
            {group === "Gateway" ? "Gateway" : "You"}
          </p>
          <div className="flex flex-col gap-0.5">
            {destinations
              .filter(
                (d) =>
                  (d.group === group ||
                    (!viewer.admin &&
                      group === "You" &&
                      d.title === "Status")) &&
                  (!viewer.serviceAdmin ||
                    d.title === "Home" ||
                    (d.group === "Gateway" &&
                      !["People", "Activity"].includes(d.title))),
              )
              .map((item) => {
                const Icon = icons[item.title];
                const active =
                  canonicalDestinationPath(pathname) ===
                  canonicalDestinationPath(item.href);
                return (
                  <Tooltip key={item.href}>
                    <TooltipTrigger asChild>
                      <Link
                        href={item.href}
                        className="shell-nav-link"
                        aria-label={item.title}
                        aria-current={active ? "page" : undefined}
                        onClick={onNavigate}
                      >
                        <Icon />
                        <span className={expanded ? "" : "shell-nav-label"}>
                          {item.title}
                        </span>
                      </Link>
                    </TooltipTrigger>
                    <TooltipContent side="right">{item.title}</TooltipContent>
                  </Tooltip>
                );
              })}
          </div>
        </div>
      ))}
    </nav>
  );
}

function AlertSlot() {
  const viewer = useViewer();
  const pathname = usePathname();
  const onStatus = canonicalDestinationPath(pathname) === "/status";
  const alert = useOwnerAlertTarget(viewer.admin && !onStatus);
  // Status owns its section markup. Resolve its visible heading after route
  // loading so the banner can hand keyboard focus to the affected section.
  useEffect(() => {
    if (!viewer.admin || canonicalDestinationPath(pathname) !== "/status")
      return;
    const focusSection = () => {
      // Engines and providers share one layer table on Status.
      const section = ["#engines", "#providers"].includes(window.location.hash)
        ? "Engines and providers"
        : null;
      if (!section) return false;
      const heading = Array.from(
        document.querySelectorAll<HTMLElement>("main h2"),
      ).find((node) => node.textContent === section);
      if (!heading) return false;
      heading.tabIndex = -1;
      heading.focus();
      heading.scrollIntoView({ block: "start" });
      return true;
    };
    const observer = new MutationObserver(() => {
      if (focusSection()) observer.disconnect();
    });
    const onHashChange = () => {
      if (!focusSection())
        observer.observe(document.body, { childList: true, subtree: true });
    };
    onHashChange();
    window.addEventListener("hashchange", onHashChange);
    return () => {
      observer.disconnect();
      window.removeEventListener("hashchange", onHashChange);
    };
  }, [pathname, viewer.admin]);
  if (!alert || onStatus || canonicalDestinationPath(pathname) === "/try") return null;
  return (
    <div
      className="bg-warning-tint text-foreground border-warning/30 border-b px-5 py-2.5 sm:px-8 lg:px-12"
    >
      <ServiceNotice message={alert.message} section={alert.section} />
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { setTheme } = useTheme();
  const {
    data: viewer,
    isPending: sessionPending,
    isError: sessionError,
    refetch: refreshSession,
    isFetching: sessionRefreshing,
  } = useViewerQuery();
  const { data: instanceData } = useQuery({
    queryKey: ["foundation", "instance"],
    queryFn: async () => {
      const response = await fetch("/.well-known/azizam");
      if (!response.ok) throw new Error("Instance details are unavailable");
      return response.json() as Promise<{
        service?: { name?: string };
        presentation?: { theme_default?: string };
      }>;
    },
  });
  // The instance name defaults to the product name; repeating it says nothing.
  const instanceName = instanceData?.service?.name?.trim();
  const instanceSubtitle =
    instanceName && instanceName.toLowerCase() !== PRODUCT_NAME.toLowerCase()
      ? instanceName
      : null;
  useEffect(() => {
    const theme = instanceData?.presentation?.theme_default;
    if (
      theme &&
      ["system", "light", "dark"].includes(theme) &&
      !localStorage.getItem("theme")
    ) {
      setTheme(theme);
    }
  }, [instanceData?.presentation?.theme_default, setTheme]);
  const [moreOpen, setMoreOpen] = useState(false);
  const [shortcut, setShortcut] = useState("Ctrl K");
  useEffect(() => {
    if (/Mac|iPhone|iPad/.test(navigator.userAgent)) setShortcut("⌘K");
  }, []);
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandMounted, setCommandMounted] = useState(false);
  // Retain the dialog after first use so closing still restores focus.
  if (commandOpen && !commandMounted) setCommandMounted(true);
  const [railOpen, setRailOpen] = useState(false);
  const navigationInvoker = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  // Public pages come from gateway/ui_pages.json. A path outside it is the
  // 404 page, which needs no session either. Home ("/") and the 404 page give
  // a signed-in person the member shell; a signed-out one sees them as is.
  const pageName = pageNameFor(pathname);
  const publicPage =
    (pageName === undefined || uiPages[pageName] === "public") &&
    !((pageName === undefined || pageName === "") && viewer?.authenticated);
  const showPublicShell =
    publicPage || sessionPending || sessionError || !viewer?.authenticated;
  const currentPath = canonicalDestinationPath(pathname);
  const returnPath = signInReturnPath(pathname, "");
  const privateSignInHref = signInHref(returnPath);
  useEffect(() => {
    if (
      !publicPage &&
      !sessionPending &&
      !sessionError &&
      !viewer?.authenticated
    ) {
      // The destination stays encoded for server validation.
      router.replace(signInHref(returnPath));
    }
  }, [
    publicPage,
    sessionPending,
    sessionError,
    viewer?.authenticated,
    returnPath,
    router,
  ]);
  const ownerPage = pageAccess(pathname) === "admin";
  const content = publicPage ? (
    children
  ) : sessionPending ? (
    <p
      role="status"
      aria-label="Loading your account"
      aria-busy="true"
      className="text-muted-foreground text-body mx-auto max-w-md py-12"
    >
      Loading your account…
    </p>
  ) : sessionError ? (
    <>
      <PageHeader
        title={
          destinations.find(
            (d) => currentPath === canonicalDestinationPath(d.href),
          )?.title ?? "Your gateway"
        }
      />
      <ErrorState
        heading="Couldn’t load your account"
        message="We couldn't tell why your account didn't load. Try again."
        onRetry={() => {
          void refreshSession();
        }}
        retrying={sessionRefreshing}
      />
    </>
  ) : !viewer?.authenticated ? (
    <p
      role="status"
      className="text-muted-foreground text-body mx-auto max-w-md py-12"
    >
      Opening sign in…
    </p>
  ) : viewer?.serviceAdmin &&
    destinations.some(
      (d) =>
        currentPath === canonicalDestinationPath(d.href) &&
        (d.group === "You" || ["People", "Activity"].includes(d.title)),
    ) ? (
    <>
      <PageHeader title="Admin key" />
      <p className="text-muted-foreground">
        This admin key manages the gateway. Sign in as a person to
        use your AI, view your library and usage, or manage people.
      </p>
      <Button variant="outline" asChild>
        <a href={privateSignInHref}>Sign in as a person</a>
      </Button>
    </>
  ) : ownerPage && !viewer?.admin ? (
    <>
      <PageHeader
        title={
          destinations.find(
            (d) => currentPath === canonicalDestinationPath(d.href),
          )?.title ?? "Your gateway"
        }
      />
      <ErrorState
        heading="Owner access required"
        message="This page is available to gateway owners and admins."
        action={{
          label: "Sign in with an owner account",
          href: privateSignInHref,
        }}
      />
    </>
  ) : (
    children
  );
  return (
    <ViewerContext.Provider
      value={
        viewer || {
          authenticated: false,
          name: "Your account",
          email: null,
          role: "member",
          admin: false,
        }
      }
    >
      <TooltipProvider>
        <a
          href="#main-content"
          className="focus:bg-background sr-only focus:not-sr-only focus:fixed focus:top-4 focus:left-4 focus:z-50 focus:rounded-md focus:p-3"
        >
          Skip to content
        </a>
        {showPublicShell ? (
          <div className="min-h-dvh">
            <header className="flex items-center justify-between px-5 py-4 sm:px-8">
              <Link
                href="/"
                className="flex min-h-11 items-center gap-2.5 text-lg font-semibold tracking-tight"
              >
                <span className="bg-chrome rounded-control flex size-8 items-center justify-center">
                  <BrandMark className="size-5" />
                </span>
                {PRODUCT_NAME}
              </Link>
              <ThemeToggle />
            </header>
            <main id="main-content" className="shell-content">
              {content}
            </main>
          </div>
        ) : (
          <div className="min-h-dvh">
            <aside className="shell-sidebar">
              <Link
                href="/"
                className="rounded-control mb-5 flex min-h-11 items-center gap-2.5 px-2.5"
                aria-label={`${PRODUCT_NAME}${instanceSubtitle ? ` ${instanceSubtitle}` : ""} Home`}
              >
                <BrandMark />
                <span className="shell-nav-label min-w-0">
                  <span className="block text-base leading-tight font-semibold tracking-tight">
                    {PRODUCT_NAME}{" "}
                  </span>
                  {instanceSubtitle && (
                    <span className="text-chrome-muted block truncate text-xs">
                      {instanceSubtitle}
                    </span>
                  )}
                </span>
              </Link>
              <button
                type="button"
                onClick={() => setCommandOpen(true)}
                aria-label="Go to"
                // A jump list, not a search field: pages search their own
                // content (Library searches creations), so this reads as a
                // navigation row with its shortcut.
                className="text-chrome-muted hover:text-chrome-foreground hover:bg-chrome-raised rounded-control mb-4 flex min-h-11 items-center justify-center gap-2.5 px-2.5 text-sm transition-colors lg:min-h-8 lg:justify-start"
              >
                <CornerDownRight
                  className="size-4 shrink-0"
                  aria-hidden="true"
                />
                <span className="shell-nav-label flex-1 text-left">Go to…</span>
                <kbd
                  aria-hidden="true"
                  className="shell-nav-label font-sans text-xs"
                >
                  {shortcut}
                </kbd>
              </button>
              <Button
                variant="ghost"
                onClick={(event) => {
                  navigationInvoker.current = event.currentTarget;
                  setRailOpen(true);
                }}
                aria-label="Expand navigation"
                aria-haspopup="dialog"
                aria-expanded={railOpen}
                className="text-chrome-muted hover:bg-chrome-raised hover:text-chrome-foreground mb-3 lg:hidden"
              >
                <Menu />
              </Button>
              <Nav />
              <div className="border-chrome-border mt-auto flex flex-col gap-1 border-t pt-3">
                <HealthLink />
                <div className="flex items-center gap-1 max-lg:flex-col">
                  <div className="min-w-0 flex-1 max-lg:w-full">
                    <AccountMenu />
                  </div>
                  <ThemeToggle chrome />
                </div>
              </div>
            </aside>
            <div className="sm:ml-(--rail-width) lg:ml-(--sidebar-width)">
              <header className="bg-chrome text-chrome-foreground border-chrome-border sticky top-0 z-30 flex h-14 items-center justify-between border-b pr-2 pl-4 sm:hidden">
                <Link
                  href="/"
                  aria-label={`${PRODUCT_NAME}${instanceSubtitle ? ` ${instanceSubtitle}` : ""} Home`}
                  className="flex min-h-11 min-w-0 items-center gap-2.5"
                >
                  <BrandMark />
                  <span className="min-w-0">
                    <span className="block text-base leading-tight font-semibold tracking-tight">
                      {PRODUCT_NAME}
                    </span>
                    {instanceSubtitle && (
                      <span className="text-chrome-muted block max-w-44 truncate text-xs">
                        {instanceSubtitle}
                      </span>
                    )}
                  </span>
                </Link>
                <div className="flex items-center">
                  {/* Home and Status open with their own verdict; the
                      header pill would say it a third time (D1.5). */}
                  {/^\/(home|status)?\/?$/.test(pathname ?? "") ? null : (
                    <HealthLink compact />
                  )}
                  <AccountMenu compact />
                </div>
              </header>
              <AlertSlot />
              <main id="main-content" className="shell-content pb-28 sm:pb-12">
                <JobNotices />
                {content}
              </main>
            </div>
            <nav
              aria-label="Phone navigation"
              className={`bg-background/95 fixed inset-x-0 bottom-0 z-30 grid backdrop-blur ${viewer?.serviceAdmin ? "grid-cols-3" : "grid-cols-4"} border-t pb-[env(safe-area-inset-bottom)] sm:hidden`}
            >
              {destinations
                .filter((d) =>
                  viewer?.serviceAdmin
                    ? ["Home", "Status"].includes(d.title)
                    : ["Home", "Library", "Apps & keys"].includes(d.title),
                )
                .map((d) => {
                  const Icon = icons[d.title];
                  return (
                    <Link
                      key={d.href}
                      href={d.href}
                      className="mobile-tab"
                      aria-current={
                        canonicalDestinationPath(pathname) ===
                        canonicalDestinationPath(d.href)
                          ? "page"
                          : undefined
                      }
                    >
                      <Icon aria-hidden="true" />
                      {d.title === "Apps & keys" ? "Apps" : d.title}
                    </Link>
                  );
                })}
              <button
                type="button"
                className="mobile-tab"
                aria-haspopup="dialog"
                aria-expanded={moreOpen}
                onClick={(event) => {
                  navigationInvoker.current = event.currentTarget;
                  setMoreOpen(true);
                }}
              >
                <Menu aria-hidden="true" />
                More
              </button>
            </nav>
          </div>
        )}
        <Sheet
          open={!showPublicShell && (moreOpen || railOpen)}
          onOpenChange={(open) => {
            if (!open) {
              setMoreOpen(false);
              setRailOpen(false);
            }
          }}
        >
          <SheetContent
            side={moreOpen ? "bottom" : "left"}
            className="max-h-[90dvh] overflow-y-auto"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              // "Go to" hands focus to its own dialog instead of the sheet invoker.
              if (!commandOpen) navigationInvoker.current?.focus();
            }}
          >
            <SheetHeader>
              <SheetTitle>Explore your gateway</SheetTitle>
              <SheetDescription className="sr-only">
                Every page, search and appearance.
              </SheetDescription>
            </SheetHeader>
            <div className="px-4 pb-6">
              <Nav
                expanded
                onNavigate={() => {
                  setMoreOpen(false);
                  setRailOpen(false);
                }}
              />
              <div className="mt-6 flex items-center justify-between gap-3 border-t pt-4">
                <Button
                  variant="outline"
                  className="flex-1"
                  onClick={() => {
                    setMoreOpen(false);
                    setRailOpen(false);
                    setCommandOpen(true);
                  }}
                >
                  <CornerDownRight />
                  Go to…
                </Button>
                <ThemeToggle />
              </div>
            </div>
          </SheetContent>
        </Sheet>
        {!showPublicShell && commandMounted && (
          <Suspense fallback={commandOpen ? <p role="status" className="bg-background fixed inset-x-4 top-20 z-50 rounded-lg border p-4">Loading navigation…</p> : null}>
            <CommandPalette
              open={commandOpen}
              setOpen={setCommandOpen}
              fallbackFocus={navigationInvoker}
              icons={icons}
            />
          </Suspense>
        )}
      </TooltipProvider>
    </ViewerContext.Provider>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  usePreloadShellExtras();
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <QueryClientProvider client={queryClient}>
        <Shell>{children}</Shell>
        <ToastGate />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
