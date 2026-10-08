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
  ChartColumn,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Cloud,
  Cpu,
  GitBranch,
  House,
  Images,
  KeyRound,
  LogOut,
  Menu,
  Moon,
  Play,
  ReceiptText,
  Search,
  Settings,
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
import { BrandMark } from "@/components/product/brand-mark";
import { PersonMark } from "@/components/product/person-mark";
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
  Activity: ReceiptText,
  Settings,
};
const queryClient = createQueryClient();
const publicPages = ["/sign-in", "/setup", "/invite", "/showcase"];

function signInReturnPath(pathname: string, search: string): string {
  if (typeof window !== "undefined") {
    const current = `${window.location.pathname}${window.location.search}`;
    if (current.startsWith("/ui/")) return current;
  }
  const normalizedPath = pathname.startsWith("/") ? pathname : `/${pathname}`;
  const uiPath = normalizedPath.startsWith("/ui/")
    ? normalizedPath
    : `/ui${normalizedPath}`;
  return `${uiPath}${search ? `?${search}` : ""}`;
}

function signInHref(returnPath: string): string {
  return `/ui/sign-in/?return_to=${encodeURIComponent(returnPath)}`;
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
    <Button variant="ghost" size="icon" aria-label="Toggle theme" onClick={toggle}>
      {icons}
    </Button>
  );
}

const roleLabels: Record<string, string> = {
  owner: "Owner",
  superadmin: "Owner",
  admin: "Admin",
};

function AccountMenu({ compact = false }: { compact?: boolean }) {
  const viewer = useViewer();
  async function signOut() {
    try {
      const member = await fetch("/me/api/session", {
        credentials: "same-origin",
        cache: "no-store",
      });
      const snapshot = await member.json();
      const csrf =
        snapshot.csrf_token || (await api.session()).actor.csrf_token;
      const response = await fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "x-azizam-csrf": csrf, accept: "application/json" },
      });
      if (!response.ok) throw new Error("Sign out failed");
      window.location.assign("/ui/sign-in/");
    } catch {
      toast.error("Could not sign out. Refresh the page and try again.");
    }
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={
            compact
              ? "flex size-11 items-center justify-center rounded-control"
              : "shell-nav-footer w-full justify-center px-2 lg:justify-start lg:px-3"
          }
          aria-label={`Account menu: ${viewer.name}`}
        >
          <PersonMark name={viewer.name} size={compact ? "md" : "sm"} />
          {!compact && (
            <span className="shell-nav-label text-chrome-foreground min-w-0 flex-1 truncate text-left font-medium">
              {viewer.name}
            </span>
          )}
          {!compact && <ChevronDown className="shell-nav-label size-4" />}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel className="font-normal">
          <span className="block font-medium">{viewer.name}</span>
          <span className="text-muted-foreground block text-xs">
            {viewer.serviceAdmin
              ? "Service key"
              : (roleLabels[viewer.role] ?? "Member")}
          </span>
        </DropdownMenuLabel>
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
          {viewer.authenticated ? (
            <DropdownMenuItem onSelect={() => void signOut()}>
              <LogOut />
              Sign out
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem asChild>
              <a
                href={`/ui/sign-in/?return_to=${encodeURIComponent(typeof window === "undefined" ? "/ui/home/" : window.location.pathname + window.location.search)}`}
              >
                <LogOut />
                Sign in
              </a>
            </DropdownMenuItem>
          )}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function HealthLink({ compact = false }: { compact?: boolean }) {
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
  const dot =
    tone === "ok"
      ? "bg-success"
      : tone === "warn"
        ? "bg-warning"
        : tone === "bad"
          ? "bg-danger"
          : "bg-neutral";
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={
            compact
              ? "text-chrome-foreground hover:bg-chrome-raised flex min-h-11 items-center gap-2 rounded-control px-2.5 text-sm"
              : "shell-nav-footer"
          }
          aria-label={`Gateway status: ${visibleLabel}`}
          title={reason}
        >
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${dot}`}
          />
          <span className={compact ? "sr-only" : "shell-nav-label"}>
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
        <p className="flex items-center gap-2 font-medium">
          <span aria-hidden="true" className={`size-2 rounded-full ${dot}`} />
          {visibleLabel}
        </p>
        <p className="text-muted-foreground text-sm">{reason}</p>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
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
          {viewer.admin && (
            <>
              <dt className="text-muted-foreground">Engines</dt>
              <dd>
                {statusError
                  ? "Couldn’t refresh"
                  : health?.observation_available
                    ? health.label || "Not checked yet"
                    : "Not checked yet"}
              </dd>
            </>
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
    <nav
      aria-label="Main navigation"
      className={`flex flex-col gap-5 ${expanded ? "sheet-nav" : ""}`}
    >
      {["You", ...(viewer.admin ? ["Gateway"] : [])].map((group) => (
        <div key={group}>
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
  if (!alert || onStatus) return null;
  return (
    <div
      className="bg-warning-tint text-foreground border-warning/30 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b px-5 py-2.5 text-sm sm:px-8 lg:px-12"
      role="status"
    >
      <span className="flex min-w-0 items-center gap-2">
        <span aria-hidden="true" className="bg-warning size-2 shrink-0 rounded-full" />
        {alert.message}
      </span>
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
      const response = await fetch("/.well-known/azizam");
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
  const returnPath = signInReturnPath(pathname, "");
  const privateSignInHref = signInHref(returnPath);
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
  ) : !viewer?.authenticated ? (
    <>
      <PageHeader
        title={
          destinations.find(
            (d) => currentPath === canonicalDestinationPath(d.href),
          )?.title ?? "Your gateway"
        }
      />
      <ErrorState
        heading="Sign in to continue"
        message="Sign in to view this page."
        action={{
          label: "Sign in",
          href: privateSignInHref,
        }}
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
        {publicPage ? (
          <div className="min-h-dvh">
            <header className="flex items-center justify-between px-5 py-4 sm:px-8">
              <Link
                href="/"
                className="flex min-h-11 items-center gap-2.5 text-lg font-semibold tracking-tight"
              >
                <span className="bg-chrome flex size-8 items-center justify-center rounded-control">
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
                className="mb-5 flex min-h-11 items-center gap-2.5 rounded-control px-2.5"
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
                aria-label="Search"
                className="border-chrome-border text-chrome-muted hover:text-chrome-foreground hover:border-chrome-muted mb-5 flex min-h-11 items-center justify-center gap-2.5 rounded-control border px-3 text-sm transition-colors lg:min-h-9 lg:justify-start"
              >
                <Search className="size-4 shrink-0" aria-hidden="true" />
                <span className="shell-nav-label flex-1 text-left">Search</span>
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
              <header className="bg-chrome text-chrome-foreground sticky top-0 z-30 flex h-14 items-center justify-between pr-2 pl-4 sm:hidden">
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
                  <HealthLink compact />
                  <AccountMenu compact />
                </div>
              </header>
              <AlertSlot />
              <main id="main-content" className="shell-content pb-28 sm:pb-12">
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
                  <Search />
                  Search
                </Button>
                <ThemeToggle />
              </div>
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
