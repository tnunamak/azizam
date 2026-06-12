"use client";

/** Admin shell — fixed left sidebar (desktop) / top bar + Sheet (mobile).
 *
 * This layout owns the QueryClientProvider and Toaster so all admin pages
 * share a single React Query cache. Nav entries cover all seven flat sections
 * per the steering doc IA. */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";

import {
  AuditIcon,
  AppsIcon,
  ExternalLinkIcon,
  IconButton,
  MenuIcon,
  OverviewIcon,
  ProfilesIcon,
  ServicesIcon,
  SettingsIcon,
  Sheet,
  Toaster,
  UsersIcon,
  XIcon,
} from "@/components/ui";
import { api, createQueryClient } from "@/lib/api";

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
      className={[
        "rounded-vf-sm flex items-center gap-2.5 px-2.5 py-2 text-sm transition-colors",
        isActive
          ? "bg-vf-accent/10 text-vf-accent font-medium"
          : "text-vf-muted hover:bg-vf-surface-2 hover:text-vf-text",
      ].join(" ")}
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
    <div className="border-vf-border border-t pt-4">
      {email && (
        <p className="text-vf-micro text-vf-subtle mb-3 truncate px-2.5 font-mono">
          {email}
        </p>
      )}
      <div className="flex flex-col gap-1">
        <a
          href="/me"
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
    <div className="flex h-full flex-col gap-6 p-4">
      {/* Wordmark + admin kicker */}
      <div className="flex flex-col gap-0.5 px-2.5">
        <Link
          href="/admin"
          className="text-vf-text text-base font-semibold leading-tight tracking-tight"
          onClick={onNav}
        >
          vivid.fish
        </Link>
        <p className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
          admin
        </p>
      </div>

      {/* Nav items */}
      <nav className="flex flex-1 flex-col gap-0.5" aria-label="Admin navigation">
        {NAV_ITEMS.map((item) => (
          <NavLink key={item.href} item={item} onClick={onNav} />
        ))}
      </nav>

      {/* Bottom: email + links */}
      <SidebarBottom />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout shell

function AdminShell({ children }: { children: React.ReactNode }) {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  return (
    <div className="bg-vf-bg min-h-screen">
      {/* Desktop: fixed left sidebar */}
      <aside className="bg-vf-surface-1 border-vf-border fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r md:flex">
        <SidebarNav />
      </aside>

      {/* Mobile: top bar */}
      <header className="bg-vf-surface-1 border-vf-border fixed inset-x-0 top-0 z-30 flex h-14 items-center justify-between border-b px-4 md:hidden">
        <Link
          href="/admin"
          className="text-vf-text text-base font-semibold tracking-tight"
        >
          vivid.fish
          <span className="text-vf-subtle ml-1.5 font-mono text-[11px] tracking-wider uppercase">
            admin
          </span>
        </Link>
        <IconButton
          aria-label="Open navigation"
          variant="ghost"
          onClick={() => setMobileNavOpen(true)}
        >
          <MenuIcon className="size-5" />
        </IconButton>
      </header>

      {/* Mobile nav sheet — rendered only when open so the fixed overlay
           does not sit on top of the desktop sidebar (z-50 fixed left-0). */}
      {mobileNavOpen && (
        <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
          <MobileNavSheet onClose={() => setMobileNavOpen(false)} />
        </Sheet>
      )}

      {/* Content column */}
      <main
        className={[
          "flex min-h-screen flex-col",
          "pt-14 md:pt-0",   // offset for mobile top bar
          "md:ml-60",         // offset for desktop sidebar
        ].join(" ")}
      >
        <div className="mx-auto w-full max-w-[1080px] px-4 py-8 md:px-10 md:py-12">
          {children}
        </div>
      </main>
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
        <span className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
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
      <SidebarNav onNav={onClose} />
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
