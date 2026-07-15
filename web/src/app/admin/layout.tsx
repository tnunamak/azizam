"use client";

/** Admin shell — fixed left sidebar (desktop) / top bar + Sheet (mobile).
 *
 * This layout owns the QueryClientProvider and Toaster so all admin pages
 * share a single React Query cache. Nav entries cover all seven flat sections
 * per the steering doc IA. */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo, useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";

import {
  AccountChip,
  AmbientBackdrop,
  AuditIcon,
  AppsIcon,
  type BadgeTone,
  BrandLockup,
  Button,
  CommandSearch,
  type CommandSearchItem,
  ExternalLinkIcon,
  IconButton,
  MenuIcon,
  MobileTabBar,
  MobileTopRail,
  OverviewIcon,
  ProfilesIcon,
  ServicesIcon,
  SettingsIcon,
  Sheet,
  StatusCluster,
  Toaster,
  UsersIcon,
  XIcon,
} from "@/components/ui";
import { api, createQueryClient, queryKeys } from "@/lib/api";
import {
  adminAppHref,
  adminProfileHref,
  adminServiceHref,
} from "@/lib/admin-links";

// ---------------------------------------------------------------------------
// Nav definition

interface NavItem {
  label: string;
  href: string;
  Icon: React.FC<React.SVGProps<SVGSVGElement>>;
}

const NAV_ITEMS: NavItem[] = [
  { label: "Overview", href: "/admin", Icon: OverviewIcon },
  { label: "Services", href: "/admin/services", Icon: ServicesIcon },
  { label: "Apps & keys", href: "/admin/apps", Icon: AppsIcon },
  { label: "Profiles", href: "/admin/profiles", Icon: ProfilesIcon },
  { label: "Users", href: "/admin/users", Icon: UsersIcon },
  { label: "Audit", href: "/admin/audit", Icon: AuditIcon },
  { label: "Settings", href: "/admin/settings", Icon: SettingsIcon },
];

// CommandSearch starts with real admin nav destinations and adds live objects.
const NAV_SEARCH_ITEMS: CommandSearchItem[] = NAV_ITEMS.map((item) => ({
  id: item.href,
  label: item.label,
  href: item.href,
  description: "section",
}));

const RESOURCE_LINKS = [
  { label: "Capabilities", href: "/gateway/v1/capabilities" },
  { label: "llms.txt", href: "/llms.txt" },
  { label: "API schema", href: "/openapi.json" },
] as const;

// ---------------------------------------------------------------------------
// Nav link (active state)

function NavLink({ item, onClick }: { item: NavItem; onClick?: () => void }) {
  const pathname = usePathname();

  // Exact match for overview; prefix match for the rest.
  // Note: usePathname() returns paths without the basePath prefix (/ui).
  const isActive =
    item.href === "/admin"
      ? pathname === "/admin" || pathname === "/admin/"
      : pathname.startsWith(item.href);

  return (
    <Link
      href={item.href}
      onClick={onClick}
      className="vf-shell-link"
      aria-current={isActive ? "page" : undefined}
    >
      <item.Icon className="size-4 shrink-0" />
      {item.label}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Sidebar bottom (session-aware)

function SidebarBottom() {
  const { data: sessionData } = useQuery({
    queryKey: ["session"],
    queryFn: api.session,
  });

  const email = sessionData?.actor.email ?? null;

  return (
    <div className="vf-sidebar-footer">
      <div className="vf-sidebar-resource-group">
        <p className="vf-sidebar-resource-label">Resources</p>
        <div className="flex flex-col gap-1">
          {RESOURCE_LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="vf-sidebar-resource-link"
              target={link.href.startsWith("http") ? "_blank" : undefined}
              rel={link.href.startsWith("http") ? "noreferrer" : undefined}
            >
              {link.label}
            </a>
          ))}
        </div>
      </div>
      {email && (
        <p className="text-vf-micro text-vf-subtle mb-3 truncate px-2.5 font-mono">
          {email}
        </p>
      )}
      <div className="flex flex-col gap-1">
        {/* Root is the server-rendered user portal, outside the static admin app. */}
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
        <a
          href="/"
          className="text-vf-muted hover:text-vf-text flex items-center gap-1.5 px-2.5 py-1.5 text-sm transition-colors"
        >
          <ExternalLinkIcon className="size-3.5 shrink-0" />
          Open portal
        </a>
        <a
          href="/me/logout"
          className="text-vf-muted hover:text-vf-text px-2.5 py-1.5 text-sm transition-colors"
        >
          Sign out
        </a>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sidebar nav content (shared by desktop + mobile Sheet)

function SidebarNav({ onNav }: { onNav?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-5">
      {/* Wordmark + admin kicker */}
      <BrandLockup href="/admin" kicker="AI Gateway" />

      {/* Nav items */}
      <nav
        className="flex flex-1 flex-col gap-0.5"
        aria-label="Admin navigation"
      >
        {NAV_ITEMS.map((item) => (
          <NavLink key={item.href} item={item} onClick={onNav} />
        ))}
      </nav>

      {/* Bottom: email + links */}
      <SidebarBottom />
    </div>
  );
}

function useShellStatus() {
  const { data: overview } = useQuery({
    queryKey: queryKeys.overview,
    queryFn: api.overview,
  });
  const { data: sessionData } = useQuery({
    queryKey: queryKeys.session,
    queryFn: api.session,
  });

  const counts = overview?.counts;
  const attention = overview?.attention ?? [];
  const hasProblem = attention.some((item) => item.severity === "problem");
  const serviceTotal = counts?.services_total ?? 0;
  const serviceEnabled = counts?.services_enabled ?? 0;
  const partialServices = serviceTotal > 0 && serviceEnabled < serviceTotal;
  const statusTone: BadgeTone = hasProblem || partialServices ? "warn" : "ok";
  const statusLabel = hasProblem
    ? "Needs attention"
    : partialServices
      ? `${serviceEnabled}/${serviceTotal} enabled`
      : "Operational";
  const email = sessionData?.actor.email ?? "Signed in";
  return { statusTone, statusLabel, email };
}

function useAdminSearchItems(): CommandSearchItem[] {
  const { data: apps } = useQuery({
    queryKey: queryKeys.apps,
    queryFn: api.apps.list,
    staleTime: 30_000,
  });
  const { data: registry } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    staleTime: 30_000,
  });
  const { data: profiles } = useQuery({
    queryKey: queryKeys.profiles,
    queryFn: api.profiles.list,
    staleTime: 30_000,
  });

  return useMemo(() => {
    const appItems: CommandSearchItem[] = (apps ?? []).map((app) => ({
      id: `app:${app.id}`,
      label: app.name,
      href: adminAppHref(app.id),
      description:
        app.disabled_at === null ? "app" : "disabled app",
    }));
    const serviceItems: CommandSearchItem[] = Object.values(
      registry?.services ?? {},
    ).map((service) => ({
      id: `service:${service.id}`,
      label: service.name,
      href: adminServiceHref(service.id),
      description: service.enabled ? "service" : "disabled service",
    }));
    const profileItems: CommandSearchItem[] = (profiles ?? []).map(
      (profile) => ({
        id: `profile:${profile.id}`,
        label: profile.name,
        href: adminProfileHref(profile.id),
        description: "profile",
      }),
    );
    return [
      ...NAV_SEARCH_ITEMS,
      ...appItems,
      ...serviceItems,
      ...profileItems,
    ];
  }, [apps, profiles, registry]);
}

function TopBar() {
  const { statusTone, statusLabel, email } = useShellStatus();
  const searchItems = useAdminSearchItems();

  return (
    <div className="vf-console-topbar hidden md:flex">
      <div className="flex min-w-0 items-center gap-3">
        <Link href="/admin/services" className="text-vf-text no-underline">
          <StatusCluster
            tone={statusTone}
            label={statusLabel}
            detail="service state"
          />
        </Link>
      </div>

      <div className="flex flex-1 justify-center px-4">
        <CommandSearch
          items={searchItems}
          placeholder="Search apps, services, profiles..."
          className="w-full max-w-xs"
        />
      </div>

      <div className="flex min-w-0 items-center gap-2">
        <Button asChild variant="secondary" size="sm">
          {/* Root is the server-rendered user portal, outside the static admin app. */}
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
          <a href="/">Portal</a>
        </Button>
        <AccountChip email={email} />
        <Button asChild variant="ghost" size="sm">
          <a href="/me/logout">Sign out</a>
        </Button>
      </div>
    </div>
  );
}

function MobileTopBar({ onOpenNav }: { onOpenNav: () => void }) {
  const { statusTone, statusLabel } = useShellStatus();

  return (
    <MobileTopRail
      className="md:hidden"
      brand={<BrandLockup href="/admin" kicker="console" />}
      account={
        <div className="flex items-center gap-2">
          <Link
            href="/admin/services"
            className="text-vf-text min-w-0 no-underline"
          >
            <StatusCluster
              tone={statusTone}
              label={statusLabel}
              className="max-w-40"
            />
          </Link>
          <IconButton
            aria-label="Open navigation"
            variant="ghost"
            onClick={onOpenNav}
          >
            <MenuIcon className="size-5" />
          </IconButton>
        </div>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// Layout shell

function AdminShell({ children }: { children: React.ReactNode }) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const mobileTabs = NAV_ITEMS.slice(0, 5).map((item) => ({
    label: item.label === "Apps & keys" ? "Apps" : item.label,
    href: item.href,
    icon: <item.Icon />,
  }));

  return (
    <div className="vf-app vf-console-shell relative overflow-x-hidden">
      <AmbientBackdrop />

      {/* Desktop rail */}
      <aside className="vf-console-rail">
        <SidebarNav />
      </aside>

      <div className="vf-console-stage">
        {/* Mobile: top bar */}
        <MobileTopBar onOpenNav={() => setMobileNavOpen(true)} />

        {/* Mobile nav sheet — overflow routes and account actions. */}
        {mobileNavOpen && (
          <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
            <MobileNavSheet onClose={() => setMobileNavOpen(false)} />
          </Sheet>
        )}

        {/* Content column */}
        <TopBar />
        <main className="vf-console-main">
          <div className="vf-console-inner">{children}</div>
        </main>
      </div>

      <MobileTabBar items={mobileTabs} />
    </div>
  );
}

// The Sheet content for mobile nav — separate component so the nav links
// can read the pathname after the sheet portal mounts.
function MobileNavSheet({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="bg-vf-surface-1 fixed inset-y-0 left-0 z-50 w-72 shadow-lg"
      role="dialog"
      aria-modal="true"
      aria-label="Admin navigation"
    >
      <div className="flex items-center justify-between p-4">
        <span className="text-vf-micro text-vf-subtle font-mono uppercase">
          Navigation
        </span>
        <IconButton
          aria-label="Close navigation"
          variant="ghost"
          size="sm"
          onClick={onClose}
        >
          <XIcon className="size-4" />
        </IconButton>
      </div>
      <div className="h-[calc(100%-4rem)] px-4 pb-4">
        <SidebarNav onNav={onClose} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Export: wraps with QueryClientProvider + Toaster so all admin pages share
// one cache. The provider must be client-side (this file is "use client").

const queryClient = createQueryClient();

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <AdminShell>{children}</AdminShell>
      <Toaster />
    </QueryClientProvider>
  );
}
