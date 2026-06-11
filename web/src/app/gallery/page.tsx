"use client";

/** Hidden component gallery — the visual test target for the ui library.
 * Renders every component in every state (hover/focus are live; loading,
 * disabled, empty, error are explicit variants). Not linked from any nav. */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  CopyButton,
  DataTable,
  type DataTableColumn,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  EmptyState,
  EntityRef,
  Field,
  IconButton,
  Input,
  OverflowMenu,
  OverflowMenuItem,
  OverflowMenuSeparator,
  RadioCard,
  RadioCards,
  RelativeTime,
  SecretMask,
  SectionHeader,
  Select,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
  Skeleton,
  Spinner,
  Switch,
  Toaster,
  toast,
  CopyIcon,
  DotsIcon,
  XIcon,
} from "@/components/ui";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-vf-border flex flex-col gap-4 border-b pb-8">
      <h2 className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Cluster({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center gap-3">{children}</div>;
}

interface DemoApp {
  id: string;
  name: string;
  kind: "service" | "personal";
  profile: string;
  keys: number;
  created_at: number;
  last_used_at: number | null;
}

export default function GalleryPage() {
  // Sample timestamps are client-only: computing them at build time would
  // bake stale values into the static export and mismatch on hydration.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Math.floor(Date.now() / 1000));
  }, []);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typedConfirmOpen, setTypedConfirmOpen] = useState(false);

  const apps: DemoApp[] = now
    ? [
        {
          id: "app_9f3k2j1h8g7f6d5s",
          name: "brother-laptop",
          kind: "personal",
          profile: "Trusted local",
          keys: 2,
          created_at: now - 86_400 * 12,
          last_used_at: now - 150,
        },
        {
          id: "app_2b4n6m8q0w1e3r5t",
          name: "telegram-bot",
          kind: "service",
          profile: "Default local",
          keys: 1,
          created_at: now - 3_600 * 5,
          last_used_at: null,
        },
        {
          id: "app_7c9v1b3n5m7k9j1h",
          name: "home-assistant",
          kind: "service",
          profile: "Guarded",
          keys: 4,
          created_at: now - 86_400 * 90,
          last_used_at: now - 86_400 * 3,
        },
      ]
    : [];

  const appColumns: DataTableColumn<DemoApp>[] = [
    {
      id: "app",
      header: "App",
      cell: (row) => (
        <span className="flex items-center gap-2">
          <EntityRef name={row.name} id={row.id} />
          <Badge sm tone={row.kind === "personal" ? "info" : "neutral"}>
            {row.kind}
          </Badge>
        </span>
      ),
    },
    { id: "profile", header: "Profile" },
    { id: "keys", header: "Keys", align: "right", hideOnMobile: true },
    { id: "created_at", header: "Created" },
    {
      id: "last_used_at",
      header: "Last used",
      cell: (row) => <RelativeTime ts={row.last_used_at} fallback="Never" />,
    },
  ];

  const rowMenu = (row: DemoApp) => (
    <OverflowMenu label={`Actions for ${row.name}`}>
      <OverflowMenuItem onSelect={() => toast(`Mint key for ${row.name}`)}>
        Mint key
      </OverflowMenuItem>
      <OverflowMenuItem
        onSelect={() => toast(`Change profile for ${row.name}`)}
      >
        Change profile
      </OverflowMenuItem>
      <OverflowMenuSeparator />
      <OverflowMenuItem danger onSelect={() => setConfirmOpen(true)}>
        Disable
      </OverflowMenuItem>
    </OverflowMenu>
  );

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-8 p-6 pb-24">
      <Toaster />
      <header className="flex flex-col gap-1">
        <p className="text-vf-micro text-vf-subtle font-mono tracking-wider uppercase">
          ui gallery
        </p>
        <h1 className="text-vf-display text-vf-text font-serif">
          Every component, every state
        </h1>
        <p className="text-vf-small text-vf-muted">
          Visual test target for web/src/components/ui — not linked from
          navigation.
        </p>
      </header>

      <Section title="type scale">
        <div className="flex flex-col gap-2">
          <p className="text-vf-display font-serif">display 28/1.1 serif</p>
          <p className="text-vf-title">title 20/1.2 sans 600</p>
          <p className="text-vf-heading">heading 16/1.3 sans 600</p>
          <p className="text-vf-body">body 14/1.5 sans</p>
          <p className="text-vf-small">small 13/1.4 sans</p>
          <p className="text-vf-micro font-mono tracking-wider uppercase">
            micro 12/1.3 mono
          </p>
        </div>
      </Section>

      <Section title="badge">
        <Cluster>
          <Badge>neutral</Badge>
          <Badge tone="ok">active</Badge>
          <Badge tone="warn">expiring</Badge>
          <Badge tone="bad">revoked</Badge>
          <Badge tone="info">personal</Badge>
          <Badge tone="accent">owner</Badge>
        </Cluster>
        <Cluster>
          <Badge sm>neutral sm</Badge>
          <Badge sm tone="ok">
            ok sm
          </Badge>
          <Badge sm tone="bad">
            bad sm
          </Badge>
        </Cluster>
      </Section>

      <Section title="button">
        <Cluster>
          <Button variant="primary">Create app</Button>
          <Button>Secondary</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="danger">Revoke</Button>
        </Cluster>
        <Cluster>
          <Button variant="primary" size="sm">
            Small
          </Button>
          <Button size="md">Medium</Button>
          <Button size="lg">Large</Button>
        </Cluster>
        <Cluster>
          <Button variant="primary" loading>
            Saving
          </Button>
          <Button loading>Loading</Button>
          <Button variant="primary" disabled>
            Disabled
          </Button>
          <Button disabled>Disabled</Button>
          <Button variant="danger" loading>
            Revoking
          </Button>
          <Button asChild variant="ghost">
            <a href="#button">As link</a>
          </Button>
        </Cluster>
      </Section>

      <Section title="icon button">
        <Cluster>
          <IconButton aria-label="More actions">
            <DotsIcon className="size-4" />
          </IconButton>
          <IconButton aria-label="Copy" variant="secondary">
            <CopyIcon className="size-4" />
          </IconButton>
          <IconButton aria-label="Remove" variant="danger">
            <XIcon className="size-4" />
          </IconButton>
          <IconButton aria-label="Copy small" size="sm" variant="secondary">
            <CopyIcon className="size-4" />
          </IconButton>
          <IconButton aria-label="Loading" loading>
            <DotsIcon className="size-4" />
          </IconButton>
          <IconButton aria-label="Disabled" disabled>
            <DotsIcon className="size-4" />
          </IconButton>
        </Cluster>
      </Section>

      <Section title="relative time">
        <Cluster>
          <RelativeTime ts={now ? now - 10 : null} fallback="…" />
          <RelativeTime ts={now ? now - 300 : null} fallback="…" />
          <RelativeTime ts={now ? now - 3_600 * 7 : null} fallback="…" />
          <RelativeTime ts={now ? now - 86_400 * 40 : null} fallback="…" />
          <RelativeTime ts={now ? now + 3_600 : null} fallback="…" />
          <RelativeTime ts={null} fallback="Never" />
        </Cluster>
        <p className="text-vf-small text-vf-subtle">
          Hover for absolute UTC; values tick on a shared 30s interval.
        </p>
      </Section>

      <Section title="entity ref">
        <Cluster>
          <EntityRef name="brother-laptop" id="app_9f3k2j1h8g7f6d5s" />
          <EntityRef
            name="Tim Nunamaker"
            id="usr_8d7f6g5h4j3k2l1a"
            href="#entity-ref"
          />
        </Cluster>
      </Section>

      <Section title="secret mask">
        <Cluster>
          <SecretMask prefix="opk_AbC123xYz789Qr" />
          <SecretMask prefix="oak_Zx9Yw8Vu7Ts6Rq" />
        </Cluster>
      </Section>

      <Section title="copy button">
        <Cluster>
          <CopyButton value="opk_example_not_a_real_key">Copy key</CopyButton>
          <CopyButton value="app_9f3k2j1h8g7f6d5s" aria-label="Copy app id" />
          <CopyButton
            value="curl -s https://example.invalid/v1/models"
            variant="ghost"
          >
            Copy curl
          </CopyButton>
        </Cluster>
      </Section>

      <Section title="field / input / select">
        <div className="grid max-w-2xl gap-4 sm:grid-cols-2">
          <Field label="App name" help="Lowercase, dashes allowed.">
            <Input placeholder="brother-laptop" />
          </Field>
          <Field label="App name" error="An app with this name already exists.">
            <Input defaultValue="brother-laptop" />
          </Field>
          <Field label="Disabled">
            <Input disabled placeholder="Not editable" />
          </Field>
          <Field label="Profile" help="default-local is the built-in fallback.">
            <Select defaultValue="default-local">
              <option value="default-local">Default local</option>
              <option value="prof_trusted">Trusted local</option>
              <option value="prof_guarded">Guarded</option>
            </Select>
          </Field>
          <Field label="Profile" error="Pick a profile.">
            <Select defaultValue="">
              <option value="" disabled>
                Choose…
              </option>
              <option value="prof_trusted">Trusted local</option>
            </Select>
          </Field>
          <Field label="Profile (disabled)">
            <Select disabled defaultValue="prof_trusted">
              <option value="prof_trusted">Trusted local</option>
            </Select>
          </Field>
        </div>
      </Section>

      <Section title="checkbox">
        <div className="flex flex-col gap-3">
          <Checkbox>Allow image generation</Checkbox>
          <Checkbox defaultChecked>Allow chat completions</Checkbox>
          <Checkbox disabled>Disabled unchecked</Checkbox>
          <Checkbox disabled defaultChecked>
            Disabled checked
          </Checkbox>
          <Cluster>
            <Checkbox aria-label="Bare checkbox" />
            <Checkbox aria-label="Bare checked" defaultChecked />
          </Cluster>
        </div>
      </Section>

      <Section title="radio cards">
        <RadioCards defaultValue="service" className="max-w-2xl">
          <RadioCard
            value="service"
            title="Shared app"
            description="One key pool for a household service. Owned by the gateway."
          />
          <RadioCard
            value="personal"
            title="Personal app"
            description="Belongs to one person; keys act as them."
          />
          <RadioCard
            value="disabled"
            title="Disabled option"
            description="Shown for the disabled state."
            disabled
          />
        </RadioCards>
      </Section>

      <Section title="switch">
        <Cluster>
          <Switch aria-label="Off" />
          <Switch aria-label="On" defaultChecked />
          <Switch aria-label="Disabled off" disabled />
          <Switch aria-label="Disabled on" disabled defaultChecked />
        </Cluster>
      </Section>

      <Section title="section header">
        <SectionHeader
          kicker="access"
          title="Apps & keys"
          description="Who can call the gateway, and with what."
          action={<Button variant="primary">Create app</Button>}
        />
      </Section>

      <Section title="data table">
        <DataTable
          label="Demo apps"
          columns={appColumns}
          rows={apps}
          rowKey={(row) => row.id}
          loading={now === null}
          rowMenu={rowMenu}
        />
        <h3 className="text-vf-small text-vf-subtle">Loading</h3>
        <DataTable
          label="Loading apps"
          columns={appColumns}
          rows={[]}
          rowKey={(row: DemoApp) => row.id}
          loading
          skeletonRows={3}
        />
        <h3 className="text-vf-small text-vf-subtle">Empty</h3>
        <DataTable
          label="No apps"
          columns={appColumns}
          rows={[]}
          rowKey={(row: DemoApp) => row.id}
          empty={
            <EmptyState action={<Button variant="primary">Create app</Button>}>
              No apps yet.
            </EmptyState>
          }
        />
      </Section>

      <Section title="overflow menu">
        <Cluster>
          <OverflowMenu label="Demo actions">
            <OverflowMenuItem onSelect={() => toast("Minted")}>
              Mint key
            </OverflowMenuItem>
            <OverflowMenuItem disabled>Change profile</OverflowMenuItem>
            <OverflowMenuSeparator />
            <OverflowMenuItem danger onSelect={() => setConfirmOpen(true)}>
              Disable
            </OverflowMenuItem>
          </OverflowMenu>
        </Cluster>
      </Section>

      <Section title="dialog">
        <Cluster>
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="primary">Open dialog</Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Create app</DialogTitle>
                <DialogDescription>
                  Apps group keys and pin a profile.
                </DialogDescription>
              </DialogHeader>
              <DialogBody>
                <Field label="Name" required>
                  <Input placeholder="brother-laptop" />
                </Field>
                <Field label="Profile">
                  <Select defaultValue="default-local">
                    <option value="default-local">Default local</option>
                    <option value="prof_trusted">Trusted local</option>
                  </Select>
                </Field>
              </DialogBody>
              <DialogFooter>
                <Button variant="ghost">Cancel</Button>
                <Button variant="primary">Create</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </Cluster>
      </Section>

      <Section title="confirm dialog">
        <Cluster>
          <Button variant="danger" onClick={() => setConfirmOpen(true)}>
            Disable app…
          </Button>
          <Button variant="danger" onClick={() => setTypedConfirmOpen(true)}>
            Revoke key (typed-name)…
          </Button>
        </Cluster>
        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title="Disable brother-laptop?"
          description="Its keys stop working immediately. You can re-enable later."
          confirmLabel="Disable app"
          onConfirm={() => {
            toast.success("App disabled", {
              action: { label: "Undo", onClick: () => toast("Re-enabled") },
            });
          }}
        />
        <ConfirmDialog
          open={typedConfirmOpen}
          onOpenChange={setTypedConfirmOpen}
          title="Revoke this key?"
          description="Revocation is permanent. Anything using this key loses access."
          confirmLabel="Revoke key"
          typedName="brother-laptop"
          onConfirm={async () => {
            await new Promise((resolve) => setTimeout(resolve, 800));
            toast.success("Key revoked");
          }}
        />
      </Section>

      <Section title="sheet">
        <Cluster>
          <Sheet>
            <SheetTrigger asChild>
              <Button>Open sheet</Button>
            </SheetTrigger>
            <SheetContent>
              <SheetHeader>
                <SheetTitle>brother-laptop</SheetTitle>
                <SheetDescription>
                  Right panel on desktop, bottom sheet on mobile.
                </SheetDescription>
              </SheetHeader>
              <div className="flex flex-col gap-3">
                <SecretMask prefix="opk_AbC123xYz789Qr" />
                <RelativeTime ts={now ? now - 150 : null} fallback="Never" />
                <Badge tone="ok">active</Badge>
              </div>
            </SheetContent>
          </Sheet>
        </Cluster>
      </Section>

      <Section title="empty state">
        <EmptyState>No activity in the last 30 days.</EmptyState>
        <EmptyState
          icon={<DotsIcon className="size-5" />}
          action={<Button variant="primary">Create app</Button>}
        >
          No apps yet.
        </EmptyState>
      </Section>

      <Section title="toast">
        <Cluster>
          <Button onClick={() => toast("Profile updated")}>Default</Button>
          <Button onClick={() => toast.success("Key minted")}>Success</Button>
          <Button onClick={() => toast.error("Could not reach the gateway")}>
            Error
          </Button>
          <Button onClick={() => toast.warning("Key expires in 3 days")}>
            Warning
          </Button>
          <Button
            onClick={() =>
              toast.success("App disabled", {
                action: { label: "Undo", onClick: () => toast("Re-enabled") },
              })
            }
          >
            With action
          </Button>
        </Cluster>
      </Section>

      <Section title="skeleton / spinner">
        <Cluster>
          <Skeleton className="h-4 w-40" />
          <Skeleton className="size-9" />
          <Spinner className="text-vf-accent" />
        </Cluster>
      </Section>
    </main>
  );
}
