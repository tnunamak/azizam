"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { ThemeProvider, useTheme } from "next-themes";
import { toast } from "sonner";
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  Box,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Fish,
  GitBranch,
  Home,
  KeyRound,
  Layers,
  LogOut,
  Moon,
  MoreHorizontal,
  Play,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { api, createQueryClient } from "@/lib/api";
import {
  canonicalDestinationPath,
  destinations,
  PRODUCT_NAME,
} from "@/lib/product";
import { gatewayHealthLabel, gatewayHealthReason } from "@/lib/gateway-health";
import { useOwnerAlertTarget } from "./use-owner-alert";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import { Toaster } from "@/components/ui/sonner";
import { ErrorState } from "@/components/product/error-state";
import { CopyField } from "@/components/product/copy-field";
import { PageHeader } from "@/components/product/page-header";
import { RelativeTime } from "@/components/product/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ViewerContext, useViewer, useViewerQuery } from "./session";
import { formatAppName, formatPersonName } from "@/lib/format";

const icons: Record<string, LucideIcon> = {
  Home,
  Models: Box,
  Workflows: GitBranch,
  Try: Play,
  Library: Layers,
  Usage: Activity,
  "Apps & keys": KeyRound,
  Status: ShieldCheck,
  Engines: Zap,
  Providers: Box,
  People: Users,
  Activity,
  Settings,
};
const queryClient = createQueryClient();
const publicPages = ["/sign-in", "/setup", "/invite", "/showcase"];

function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="Toggle theme"
      onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
    >
      <Sun className="dark:hidden" />
      <Moon className="hidden dark:block" />
    </Button>
  );
}

function AccountMenu() {
  const viewer = useViewer();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="max-w-full sm:w-full sm:justify-start"
          aria-label={`Account menu: ${viewer.name}`}
        >
          <Avatar className="size-7" aria-hidden="true">
            <AvatarFallback>
              {viewer.name.slice(0, 1).toUpperCase()}
            </AvatarFallback>
          </Avatar>
          <span className="shell-nav-label min-w-0 flex-1 truncate text-left">
            {viewer.name}
          </span>
          <ChevronDown className="shell-nav-label" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{viewer.name}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          {!viewer.serviceAdmin && (
            <DropdownMenuItem asChild>
              <Link href="/apps">
                <KeyRound />
                Apps & keys
              </Link>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem asChild>
            <Link href="/design">
              <BookOpen />
              Design system
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <a href="/docs">
              <CircleHelp />
              API reference
            </a>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <a
              href={
                viewer.authenticated
                  ? "/logout"
                  : `/ui/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/ui/home/" : window.location.pathname + window.location.search)}`
              }
            >
              <LogOut />
              {viewer.authenticated ? "Sign out" : "Sign in"}
            </a>
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function HealthLink() {
  const viewer = useViewer();
  const { data: statusData, isError: statusError } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled: viewer.admin,
    refetchInterval: 60000,
  });
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
  const health = statusData?.current_health;
  const { data: memberHome } = useQuery({
    queryKey: ["foundation", "member-health"],
    queryFn: async () => {
      const response = await fetch("/me/api/home");
      if (!response.ok) throw new Error("Model availability is unavailable");
      return response.json() as Promise<{
        status?: { state: string; reason: string };
      }>;
    },
    enabled: viewer.authenticated && !viewer.admin,
    refetchInterval: 60000,
  });
  // /health reports gateway reachability only; it does not prove engine health.
  const { label, tone } = gatewayHealthLabel({
    admin: false,
    health,
    statusError: statusError,
    reachable: Boolean(gatewayData),
    reachabilityError: gatewayError,
  });
  const reason = gatewayHealthReason({
    admin: viewer.admin,
    health,
    statusError,
    reachable: Boolean(gatewayData),
    reachabilityError: gatewayError,
    memberStatus: memberHome?.status,
  });
  const visibleLabel = label;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="shell-nav-footer"
          aria-label={`Gateway status: ${visibleLabel}`}
          title={reason}
        >
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${tone === "ok" ? "bg-success" : tone === "warn" ? "bg-warning" : tone === "bad" ? "bg-danger" : "bg-muted-foreground"}`}
          />
          <span className="shell-nav-label">{visibleLabel}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        className="max-w-[calc(100vw-2rem)] space-y-3"
        aria-label="Gateway health"
      >
        <p className="font-medium">{visibleLabel}</p>
        {gatewayData && !gatewayError && (
          <p className="text-muted-foreground text-sm">
            The gateway is reachable.
          </p>
        )}
        <p className="text-muted-foreground text-sm">{reason}</p>
        <dl className="text-muted-foreground space-y-2 text-sm">
          <div>
            <dt>Last successful gateway check</dt>
            <dd>
              <RelativeTime
                ts={gatewayCheckedAt || null}
                fallback="Not measured yet"
              />
            </dd>
          </div>
          {viewer.admin && (
            <div>
              <dt>Engine observation</dt>
              <dd>
                {statusError
                  ? "Couldn’t refresh status"
                  : health?.observation_available
                    ? health.label || "Status unknown"
                    : "Status unknown"}
              </dd>
              <dd>Observation time is not provided by the gateway.</dd>
            </div>
          )}
        </dl>
        <Button variant="outline" size="sm" asChild>
          <Link href="/status">
            View status
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
    <nav aria-label="Main navigation" className="flex flex-col gap-6">
      {["You", ...(viewer.admin ? ["Gateway"] : [])].map((group) => (
        <div key={group}>
          <p
            className={
              expanded
                ? "text-muted-foreground mb-2 px-3 text-xs font-medium"
                : "shell-group-label"
            }
          >
            {group}
          </p>
          <div className="flex flex-col gap-1">
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
  const alert = useOwnerAlertTarget(viewer.admin);
  const pathname = usePathname();
  // Status owns its section markup. Resolve its visible heading after route
  // loading so the banner can hand keyboard focus to the affected section.
  useEffect(() => {
    if (!viewer.admin || canonicalDestinationPath(pathname) !== "/status")
      return;
    const focusSection = () => {
      const section =
        window.location.hash === "#engines"
          ? "Engines"
          : window.location.hash === "#providers"
            ? "Providers"
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
  if (!alert) return null;
  return (
    <div
      className="bg-warning-tint text-warning flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3 text-sm sm:px-8"
      role="status"
    >
      <span>{alert.message}</span>
      <Button variant="ghost" size="sm" asChild>
        <Link
          href={`/status#${alert.section.toLowerCase()}`}
          onClick={() => {
            // Repeated activation on Status must still move focus.
            if (canonicalDestinationPath(pathname) === "/status")
              window.dispatchEvent(new HashChangeEvent("hashchange"));
          }}
        >
          View status
          <ChevronRight />
        </Link>
      </Button>
    </div>
  );
}

function CommandPalette({
  open,
  setOpen,
}: {
  open: boolean;
  setOpen: (open: boolean) => void;
}) {
  const viewer = useViewer();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [copyAddress, setCopyAddress] = useState<string | null>(null);
  const [addressError, setAddressError] = useState(false);
  const [previousOpen, setPreviousOpen] = useState(open);
  const addressRecovery = useRef<HTMLDivElement>(null);
  const commandInvoker = useRef<HTMLElement | null>(null);
  if (previousOpen !== open) {
    setPreviousOpen(open);
    setCopyAddress(null);
    setAddressError(false);
  }
  useEffect(() => {
    if (copyAddress === null) return;
    const input = addressRecovery.current?.querySelector("input");
    input?.focus();
    input?.select();
  }, [copyAddress]);
  const { data: appData } = useQuery({
    queryKey: ["foundation", "command-apps"],
    queryFn: async () => {
      const r = await fetch("/me/api/session");
      if (!r.ok) throw new Error();
      return r.json() as Promise<{
        user?: { email?: string };
        apps: Array<{ id: string; name: string }>;
      }>;
    },
    enabled: open && viewer.authenticated && !viewer.serviceAdmin,
  });
  const { data: peopleData } = useQuery({
    queryKey: ["foundation", "command-people"],
    queryFn: api.users.list,
    enabled: open && viewer.admin && !viewer.serviceAdmin,
  });
  const { data: modelData } = useQuery({
    queryKey: ["foundation", "command-models"],
    queryFn: async () => {
      const registry = await api.services.registry();
      const names = Object.values(registry.services).flatMap((service) =>
        (service.config.models || [])
          .map((model) => model.model_group)
          .filter(Boolean),
      );
      return {
        aliases: Object.fromEntries(
          [...new Set(names)].map((name) => [name, true]),
        ),
      };
    },
    enabled: open && viewer.admin,
  });
  const go = (href: string) => {
    setOpen(false);
    router.push(href);
  };
  async function copyGatewayAddress() {
    setBusy(true);
    setAddressError(false);
    setCopyAddress(null);
    try {
      const response = await fetch("/.well-known/ai-gateway");
      if (!response.ok) throw new Error();
      const discovery = await response.json();
      const base =
        discovery.recipes?.openai?.canonical_base_url ||
        `${discovery.service?.base_url || window.location.origin}/openai/v1`;
      try {
        await navigator.clipboard.writeText(base);
        toast.success("Gateway address copied to clipboard.");
        setOpen(false);
      } catch {
        setCopyAddress(base);
      }
    } catch {
      setAddressError(true);
    } finally {
      setBusy(false);
    }
  }
  async function testConnection() {
    setBusy(true);
    try {
      const response = await fetch("/health", { cache: "no-store" });
      if (!response.ok) throw new Error();
      const result = await response.json();
      if (result.status !== "healthy") throw new Error();
      toast.success(
        "The gateway is reachable. Engine requests are tested on Try.",
      );
      setOpen(false);
    } catch {
      toast.error("The gateway could not be reached. Try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        className="overflow-hidden p-0 sm:max-w-xl"
        showCloseButton={false}
        onOpenAutoFocus={() => {
          commandInvoker.current =
            document.activeElement instanceof HTMLElement
              ? document.activeElement
              : null;
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          commandInvoker.current?.focus();
        }}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>Search your gateway</DialogTitle>
          <DialogDescription>
            Find pages, models, apps and people, or run an action.
          </DialogDescription>
        </DialogHeader>
        <Command>
          <CommandInput
            placeholder="Search pages, models, apps…"
            aria-label="Search your gateway"
          />
          <CommandList>
            <CommandEmpty>No matches. Try a page or app name.</CommandEmpty>
            <CommandGroup heading="Pages">
              {destinations
                .filter(
                  (d) =>
                    (d.group === "You" ||
                      d.title === "Status" ||
                      viewer.admin) &&
                    (!viewer.serviceAdmin ||
                      d.title === "Home" ||
                      (d.group === "Gateway" &&
                        !["People", "Activity"].includes(d.title))),
                )
                .map((d) => {
                  const Icon = icons[d.title];
                  return (
                    <CommandItem key={d.href} onSelect={() => go(d.href)}>
                      <Icon />
                      {d.title}
                    </CommandItem>
                  );
                })}
            </CommandGroup>
            {appData?.apps.length ? (
              <CommandGroup heading="Apps">
                {appData.apps.map((app) => (
                  <CommandItem
                    key={app.id}
                    onSelect={() =>
                      go(`/apps?app=${encodeURIComponent(app.id)}`)
                    }
                  >
                    <KeyRound />
                    {formatAppName(app.name, appData.user?.email)}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {Object.keys(modelData?.aliases || {}).length ? (
              <CommandGroup heading="Models">
                {Object.keys(modelData?.aliases || {}).map((name) => (
                  <CommandItem
                    key={name}
                    onSelect={() =>
                      go(`/models?model=${encodeURIComponent(name)}`)
                    }
                  >
                    <Box />
                    {name}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {peopleData?.length ? (
              <CommandGroup heading="People">
                {peopleData.map((person) => (
                  <CommandItem
                    key={person.id}
                    value={person.id}
                    keywords={[
                      person.display_name || "",
                      person.name || "",
                      person.email || "",
                    ]}
                    onSelect={() =>
                      go(`/people?person=${encodeURIComponent(person.id)}`)
                    }
                  >
                    <Users />
                    {formatPersonName(
                      person.display_name,
                      formatPersonName(person.name, "Person"),
                    )}
                    {person.email ? (
                      <span className="text-muted-foreground truncate text-xs">
                        {" "}
                        {person.email}
                      </span>
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            <CommandSeparator />
            <CommandGroup heading="Actions">
              <CommandItem
                onSelect={() => go("/apps?action=create-key")}
                disabled={!viewer.authenticated || viewer.serviceAdmin}
              >
                <KeyRound />
                Create key
              </CommandItem>
              <CommandItem onSelect={copyGatewayAddress} disabled={busy}>
                <ArrowUpRight />
                Copy gateway address
              </CommandItem>
              <CommandItem onSelect={testConnection} disabled={busy}>
                <Zap />
                Check gateway reachability
              </CommandItem>
            </CommandGroup>
          </CommandList>
        </Command>
        {copyAddress !== null && (
          <div ref={addressRecovery} className="space-y-3 border-t p-4">
            <p role="alert" className="text-destructive text-sm">
              Copy failed. Select the gateway address and copy it manually.
            </p>
            <CopyField value={copyAddress} />
          </div>
        )}
        {addressError && (
          <div className="border-t p-4">
            <ErrorState
              heading="Couldn’t load the gateway address"
              message="The gateway address could not be loaded. Check your connection and try again."
              action={{
                label: "Try again",
                onClick: () => void copyGatewayAddress(),
              }}
              retrying={busy}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
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
      const response = await fetch("/.well-known/ai-gateway");
      if (!response.ok) throw new Error("Instance details are unavailable");
      return response.json() as Promise<{
        service?: { name?: string };
        presentation?: { theme_default?: string };
      }>;
    },
  });
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
  const [commandOpen, setCommandOpen] = useState(false);
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
  const publicPage = publicPages.some((route) => pathname.startsWith(route));
  const currentPath = canonicalDestinationPath(pathname);
  const ownerPage = destinations.some(
    (d) =>
      d.group === "Gateway" &&
      d.title !== "Status" &&
      currentPath === canonicalDestinationPath(d.href),
  );
  const content = publicPage ? (
    children
  ) : sessionPending ? (
    <div
      role="status"
      aria-busy="true"
      aria-label="Loading your account"
      className="flex flex-col gap-4"
    >
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-72 max-w-full" />
      <Skeleton className="mt-8 h-40 w-full" />
    </div>
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
        message="Check your connection and try again."
        onRetry={() => {
          void refreshSession();
        }}
        retrying={sessionRefreshing}
      />
    </>
  ) : viewer?.serviceAdmin &&
    destinations.some(
      (d) =>
        currentPath === canonicalDestinationPath(d.href) &&
        (d.group === "You" || ["People", "Activity"].includes(d.title)),
    ) ? (
    <>
      <PageHeader title="Service admin session" />
      <p className="text-muted-foreground">
        This session uses a key to manage the gateway. Sign in as a person to
        use your AI, view your library and usage, or manage people.
      </p>
      <Button variant="outline" asChild>
        <a href="/ui/sign-in/">Sign in as a person</a>
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
          href: "/ui/sign-in/",
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
        {publicPage ? (
          <div className="min-h-dvh">
            <header className="flex items-center justify-between px-5 py-4">
              <Link
                href="/"
                className="flex min-h-11 items-center gap-2 font-semibold"
              >
                <Fish className="text-primary size-5" />
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
            <aside className="bg-sidebar fixed inset-y-0 left-0 hidden w-20 flex-col overflow-y-auto border-r p-3 sm:flex lg:w-60 lg:p-4">
              <Link
                href="/"
                className="mb-8 flex min-h-11 items-center gap-3 px-3 font-semibold"
                aria-label={`${PRODUCT_NAME}${instanceData?.service?.name ? ` ${instanceData.service.name}` : ""} Home`}
              >
                <Fish className="text-primary size-5 shrink-0" />
                <span className="shell-nav-label min-w-0">
                  <span className="block">{PRODUCT_NAME} </span>
                  {instanceData?.service?.name && (
                    <span className="text-muted-foreground block truncate text-xs font-normal">
                      {instanceData.service.name}
                    </span>
                  )}
                </span>
              </Link>
              <Button
                variant="ghost"
                onClick={(event) => {
                  navigationInvoker.current = event.currentTarget;
                  setRailOpen(true);
                }}
                aria-label="Expand navigation"
                aria-haspopup="dialog"
                aria-expanded={railOpen}
                className="mb-4 lg:hidden"
              >
                <MoreHorizontal />
              </Button>
              <Nav />
              <div className="mt-auto flex flex-col gap-2 pt-6">
                <Button
                  variant="ghost"
                  className="justify-start"
                  onClick={() => setCommandOpen(true)}
                  aria-label="Search"
                >
                  <Search />
                  <span className="shell-nav-label">
                    Search
                    <span
                      aria-hidden="true"
                      className="text-muted-foreground ml-12 text-xs"
                    >
                      ⌘K
                    </span>
                  </span>
                </Button>
                <div className="flex items-center gap-2 lg:px-1">
                  <ThemeToggle />
                  <span className="shell-nav-label text-muted-foreground text-xs">
                    Appearance
                  </span>
                </div>
                <AccountMenu />
                <HealthLink />
              </div>
            </aside>
            <div className="sm:ml-20 lg:ml-60">
              <header className="flex h-16 items-center justify-between border-b px-5 sm:hidden">
                <Link
                  href="/"
                  aria-label={`${PRODUCT_NAME}${instanceData?.service?.name ? ` ${instanceData.service.name}` : ""} Home`}
                  className="flex min-h-11 items-center gap-2 font-semibold"
                >
                  <Fish className="text-primary size-5" />
                  <span className="min-w-0">
                    <span className="block">{PRODUCT_NAME}</span>
                    {instanceData?.service?.name && (
                      <span className="text-muted-foreground block max-w-44 truncate text-xs font-normal">
                        {instanceData.service.name}
                      </span>
                    )}
                  </span>
                </Link>
                <div className="flex items-center">
                  <HealthLink />
                  <AccountMenu />
                </div>
              </header>
              <AlertSlot />
              <main id="main-content" className="shell-content pb-28 sm:pb-12">
                {content}
              </main>
            </div>
            <nav
              aria-label="Phone navigation"
              className={`bg-background fixed inset-x-0 bottom-0 grid ${viewer?.serviceAdmin ? "grid-cols-3" : "grid-cols-4"} border-t pb-[env(safe-area-inset-bottom)] sm:hidden`}
            >
              {destinations
                .filter(
                  (d) =>
                    ["Home", "Apps & keys", "Status"].includes(d.title) &&
                    (!viewer?.serviceAdmin || d.title !== "Apps & keys"),
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
                      <Icon />
                      {d.title}
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
                <MoreHorizontal />
                More
              </button>
            </nav>
          </div>
        )}
        <Sheet
          open={moreOpen || railOpen}
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
              // Search hands focus to its own dialog instead of the sheet invoker.
              if (!commandOpen) navigationInvoker.current?.focus();
            }}
          >
            <SheetHeader>
              <SheetTitle>Explore your gateway</SheetTitle>
              <SheetDescription>
                Pages and account actions in one place.
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
              <div className="mt-6 flex items-center justify-between">
                <ThemeToggle />
                <Button
                  variant="outline"
                  onClick={() => {
                    setMoreOpen(false);
                    setRailOpen(false);
                    setCommandOpen(true);
                  }}
                >
                  <Search />
                  Search
                </Button>
              </div>
              <AccountMenu />
            </div>
          </SheetContent>
        </Sheet>
        <CommandPalette open={commandOpen} setOpen={setCommandOpen} />
      </TooltipProvider>
    </ViewerContext.Provider>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <QueryClientProvider client={queryClient}>
        <Shell>{children}</Shell>
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
