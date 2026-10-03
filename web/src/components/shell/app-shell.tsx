"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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
import { destinations, PRODUCT_NAME } from "@/lib/product";
import { gatewayHealthLabel } from "@/lib/gateway-health";
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
import { Skeleton } from "@/components/ui/skeleton";
import { ViewerContext, useViewer, useViewerQuery } from "./session";

const icons: Record<string, LucideIcon> = {
  Home,
  Models: Box,
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
          aria-label="Account menu"
        >
          <Avatar className="size-7">
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
          <DropdownMenuItem asChild>
            <Link href="/apps">
              <KeyRound />
              Apps & keys
            </Link>
          </DropdownMenuItem>
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
            <a href={viewer.authenticated ? "/logout" : "/me/login"}>
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
  const { data: gatewayData, isError: gatewayError } = useQuery({
    queryKey: ["foundation", "reachability"],
    queryFn: async () => {
      const r = await fetch("/health");
      if (!r.ok) throw new Error("Gateway is unreachable");
      return r.json();
    },
    refetchInterval: 60000,
  });
  const health = statusData?.current_health;
  // /health reports gateway reachability only; it does not prove engine health.
  const { label, tone } = gatewayHealthLabel({
    admin: viewer.admin,
    health,
    statusError: statusError,
    reachable: Boolean(gatewayData),
    reachabilityError: gatewayError,
  });
  return (
    <Link
      href={viewer.admin ? "/status" : "/"}
      className="shell-nav-footer"
      title={health?.detail || label}
      aria-label={`Gateway status: ${label}`}
    >
      <span
        className={`size-2 shrink-0 rounded-full ${tone === "ok" ? "bg-success" : tone === "warn" ? "bg-warning" : tone === "bad" ? "bg-danger" : "bg-muted-foreground"}`}
      />
      <span className="shell-nav-label">{label}</span>
    </Link>
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
              .filter((d) => d.group === group)
              .map((item) => {
                const Icon = icons[item.title];
                const active =
                  pathname.replace(/\/$/, "") === item.href.replace(/\/$/, "");
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
  const { data } = useQuery({
    queryKey: ["foundation", "health"],
    queryFn: api.status,
    enabled: viewer.admin,
  });
  // Only enabled engines with a current failed probe are owner actionable.
  // Hardware signals and configuration coverage never enter this global slot.
  const failed = data?.current_health?.services.find(
    (service) => service.enabled && service.observation === "down",
  );
  if (!viewer.admin || !failed) return null;
  return (
    <div
      className="bg-warning-tint text-warning flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3 text-sm sm:px-8"
      role="status"
    >
      <span>{failed.name} is unavailable. Check its connection on Status.</span>
      <Button variant="ghost" size="sm" asChild>
        <Link href="/status">
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
  const { data: appData } = useQuery({
    queryKey: ["foundation", "command-apps"],
    queryFn: async () => {
      const r = await fetch("/me/api/session");
      if (!r.ok) throw new Error();
      return r.json() as Promise<{ apps: Array<{ id: string; name: string }> }>;
    },
    enabled: open && viewer.authenticated,
  });
  const { data: peopleData } = useQuery({
    queryKey: ["foundation", "command-people"],
    queryFn: api.users.list,
    enabled: open && viewer.admin,
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
  async function copyBaseUrl() {
    setBusy(true);
    try {
      const response = await fetch("/.well-known/ai-gateway");
      if (!response.ok) throw new Error();
      const discovery = await response.json();
      const base =
        discovery.recipes?.openai?.canonical_base_url ||
        `${discovery.service?.base_url || window.location.origin}/openai/v1`;
      await navigator.clipboard.writeText(base);
      toast.success("Base URL copied");
      setOpen(false);
    } catch {
      toast.error("The base URL could not be copied. Try again.");
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
                .filter((d) => d.group === "You" || viewer.admin)
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
                    {app.name}
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
                    onSelect={() =>
                      go(`/people?person=${encodeURIComponent(person.id)}`)
                    }
                  >
                    <Users />
                    {person.display_name ||
                      person.name ||
                      person.email ||
                      "Person"}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            <CommandSeparator />
            <CommandGroup heading="Actions">
              <CommandItem
                onSelect={() => go("/apps?action=create-key")}
                disabled={!viewer.authenticated}
              >
                <KeyRound />
                Create key
              </CommandItem>
              <CommandItem onSelect={copyBaseUrl} disabled={busy}>
                <ArrowUpRight />
                Copy base URL
              </CommandItem>
              <CommandItem onSelect={testConnection} disabled={busy}>
                <Zap />
                Check gateway connection
              </CommandItem>
            </CommandGroup>
          </CommandList>
        </Command>
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
  const ownerPage = destinations.some(
    (d) => d.group === "Gateway" && pathname.replace(/\/$/, "") === d.href,
  );
  const content = publicPage ? (
    children
  ) : sessionPending ? (
    <div aria-label="Loading your account" className="flex flex-col gap-4">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-72 max-w-full" />
      <Skeleton className="mt-8 h-40 w-full" />
    </div>
  ) : sessionError ? (
    <ErrorState
      message="Your account could not be loaded. Check your connection and try again."
      onRetry={() => {
        void refreshSession();
      }}
      retrying={sessionRefreshing}
    />
  ) : ownerPage && !viewer?.admin ? (
    <ErrorState message="This page is available to gateway owners and admins. Sign in with an owner account to continue." />
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
                aria-label={`${PRODUCT_NAME} Home`}
              >
                <Fish className="text-primary size-5 shrink-0" />
                <span className="shell-nav-label min-w-0">
                  <span className="block">{PRODUCT_NAME}</span>
                  {instanceData?.service?.name && (
                    <span className="text-muted-foreground block truncate text-xs font-normal">
                      {instanceData.service.name}
                    </span>
                  )}
                </span>
              </Link>
              <Button
                variant="ghost"
                onClick={() => setRailOpen(true)}
                aria-label="Expand navigation"
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
                  aria-label="Open command palette"
                >
                  <Search />
                  <span className="shell-nav-label">
                    Search
                    <span className="text-muted-foreground ml-12 text-xs">
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
                  className="flex min-h-11 items-center gap-2 font-semibold"
                >
                  <Fish className="text-primary size-5" />
                  {PRODUCT_NAME}
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
              className="bg-background fixed inset-x-0 bottom-0 grid grid-cols-4 border-t pb-[env(safe-area-inset-bottom)] sm:hidden"
            >
              {destinations
                .filter((d) => ["Home", "Models", "Library"].includes(d.title))
                .map((d) => {
                  const Icon = icons[d.title];
                  return (
                    <Link
                      key={d.href}
                      href={d.href}
                      className="mobile-tab"
                      aria-current={
                        pathname.replace(/\/$/, "") ===
                        d.href.replace(/\/$/, "")
                          ? "page"
                          : undefined
                      }
                    >
                      <Icon />
                      {d.title}
                    </Link>
                  );
                })}
              <button className="mobile-tab" onClick={() => setMoreOpen(true)}>
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
  const pathname = usePathname();
  // Retain the old operator surfaces until their replacement lanes land.
  const legacy = pathname.startsWith("/admin");
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <QueryClientProvider client={queryClient}>
        {legacy ? children : <Shell>{children}</Shell>}
        {!legacy && <Toaster />}
      </QueryClientProvider>
    </ThemeProvider>
  );
}
