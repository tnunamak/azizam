import Link from "next/link";

import {
  AmbientBackdrop,
  AppsIcon,
  AuditIcon,
  Badge,
  BrandLockup,
  Button,
  ExternalLinkIcon,
  ObjectList,
  ObjectRow,
  ProductPanel,
  ProfilesIcon,
  SectionActionHeader,
  ServicesIcon,
  SettingsIcon,
  StatusDotLabel,
  UsersIcon,
} from "@/components/ui";

export default function Home() {
  return (
    <main className="vf-app relative min-h-dvh overflow-hidden">
      <AmbientBackdrop />
      <div className="mx-auto flex min-h-dvh w-full max-w-6xl flex-col px-5 py-6 md:px-8 md:py-8">
        <header className="flex items-center justify-between gap-4">
          <BrandLockup href="/admin" />
          <StatusDotLabel tone="ok" label="local" pulse />
        </header>

        <section className="grid flex-1 items-center gap-6 py-10 lg:grid-cols-[minmax(0,0.9fr)_minmax(360px,0.7fr)]">
          <ProductPanel tone="control" className="flex flex-col gap-6">
            <SectionActionHeader
              kicker="select surface"
              title="Gateway access and operations"
              description="Admins manage local services, profiles, apps, and keys. Users manage runtime keys from their portal."
            />

            <div className="grid gap-3 sm:grid-cols-2">
              <SurfaceLink
                href="/admin"
                title="Operator console"
                description="Services, apps, keys, profiles, users, audit, and settings."
                action="Open console"
                primary
              />
              <SurfaceLink
                href="/"
                title="User portal"
                description="Runtime keys and access available to the signed-in account."
                action="Open portal"
              />
            </div>
          </ProductPanel>

          <ProductPanel tight>
            <SectionActionHeader
              title="Operator console"
              description="The admin surface is organized around the core control jobs."
              className="mb-3"
            />
            <ObjectList>
              <ObjectRow
                href="/admin/services"
                icon={<ServicesIcon className="size-4" />}
                title="Services"
                meta="Backends, model mappings, and availability"
                side={<Badge sm>live</Badge>}
              />
              <ObjectRow
                href="/admin/apps"
                icon={<AppsIcon className="size-4" />}
                title="Apps & keys"
                meta="Runtime callers and credentials"
                side={<Badge sm>access</Badge>}
              />
              <ObjectRow
                href="/admin/profiles"
                icon={<ProfilesIcon className="size-4" />}
                title="Profiles"
                meta="Access policies and capability controls"
                side={<Badge sm>policy</Badge>}
              />
              <ObjectRow
                href="/admin/users"
                icon={<UsersIcon className="size-4" />}
                title="Users"
                meta="OIDC users and default access"
                side={<Badge sm>identity</Badge>}
              />
              <ObjectRow
                href="/admin/audit"
                icon={<AuditIcon className="size-4" />}
                title="Audit"
                meta="Control-plane change history"
                side={<Badge sm>log</Badge>}
              />
              <ObjectRow
                href="/admin/settings"
                icon={<SettingsIcon className="size-4" />}
                title="Settings"
                meta="Sign-in, extensions, and media controls"
                side={<Badge sm>config</Badge>}
              />
            </ObjectList>
          </ProductPanel>
        </section>
      </div>
    </main>
  );
}

function SurfaceLink({
  href,
  title,
  description,
  action,
  primary = false,
}: {
  href: string;
  title: string;
  description: string;
  action: string;
  primary?: boolean;
}) {
  const className = "vf-action-card";
  const content = (
    <>
      <span className="flex min-w-0 flex-col gap-2">
        <span className="text-vf-heading text-vf-text">{title}</span>
        <span className="text-vf-small text-vf-muted">{description}</span>
      </span>
      <Button asChild variant={primary ? "primary" : "secondary"} size="sm">
        <span>
          {action}
          <ExternalLinkIcon className="size-3.5" />
        </span>
      </Button>
    </>
  );

  if (href === "/") {
    return (
      <a href={href} className={className}>
        {content}
      </a>
    );
  }

  return (
    <Link href={href} className={className}>
      {content}
    </Link>
  );
}
