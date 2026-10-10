"use client";

import * as React from "react";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { api, queryKeys } from "@/lib/api";
import { destinations } from "@/lib/product";
import { useViewer } from "@/components/shell/session";
import { formatAppName, formatPersonName } from "@/lib/format";
import { CopyField } from "@/components/product/copy-field";
import { ErrorState } from "@/components/product/error-state";
import { Command as CommandPrimitive } from "cmdk";
import { cn } from "@/lib/cn";
import {
  ArrowUpRight,
  Box,
  KeyRound,
  SearchIcon,
  UserRoundCog,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

function Command({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      data-slot="command"
      className={cn(
        "bg-popover text-popover-foreground flex h-full w-full flex-col overflow-hidden rounded-md",
        className,
      )}
      {...props}
    />
  );
}

function CommandDialog({
  title = "Command Palette",
  description = "Search for a command to run...",
  children,
  className,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof Dialog> & {
  title?: string;
  description?: string;
  className?: string;
  showCloseButton?: boolean;
}) {
  return (
    <Dialog {...props}>
      <DialogContent
        className={cn("overflow-hidden p-0", className)}
        showCloseButton={showCloseButton}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <Command className="[&_[cmdk-group-heading]]:text-muted-foreground **:data-[slot=command-input-wrapper]:h-12 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group]]:px-2 [&_[cmdk-group]:not([hidden])_~[cmdk-group]]:pt-0 [&_[cmdk-input-wrapper]_svg]:h-5 [&_[cmdk-input-wrapper]_svg]:w-5 [&_[cmdk-input]]:h-12 [&_[cmdk-item]]:px-2 [&_[cmdk-item]]:py-3 [&_[cmdk-item]_svg]:h-5 [&_[cmdk-item]_svg]:w-5">
          {children}
        </Command>
      </DialogContent>
    </Dialog>
  );
}

function CommandInput({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Input>) {
  return (
    <div
      data-slot="command-input-wrapper"
      className="flex h-9 items-center gap-2 border-b px-3"
    >
      <SearchIcon className="size-4 shrink-0 opacity-50" />
      <CommandPrimitive.Input
        data-slot="command-input"
        className={cn(
          "placeholder:text-muted-foreground flex h-10 w-full rounded-md bg-transparent py-3 text-sm outline-hidden disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
        {...props}
      />
    </div>
  );
}

function CommandList({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.List>) {
  return (
    <CommandPrimitive.List
      data-slot="command-list"
      className={cn(
        "max-h-75 scroll-py-1 overflow-x-hidden overflow-y-auto",
        className,
      )}
      {...props}
    />
  );
}

function CommandEmpty({
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      data-slot="command-empty"
      className="py-6 text-center text-sm"
      {...props}
    />
  );
}

function CommandGroup({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      data-slot="command-group"
      className={cn(
        "text-foreground [&_[cmdk-group-heading]]:text-muted-foreground overflow-hidden p-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium",
        className,
      )}
      {...props}
    />
  );
}

function CommandSeparator({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator
      data-slot="command-separator"
      className={cn("bg-border -mx-1 h-px", className)}
      {...props}
    />
  );
}

function CommandItem({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      data-slot="command-item"
      className={cn(
        "data-[selected=true]:bg-accent data-[selected=true]:text-accent-foreground [&_svg:not([class*='text-'])]:text-muted-foreground relative flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-hidden select-none data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className,
      )}
      {...props}
    />
  );
}

function CommandShortcut({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="command-shortcut"
      className={cn(
        "text-muted-foreground ml-auto text-xs tracking-widest",
        className,
      )}
      {...props}
    />
  );
}

export {
  Command,
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandShortcut,
  CommandSeparator,
};

export function CommandPalette({
  open,
  setOpen,
  fallbackFocus,
  icons,
}: {
  open: boolean;
  icons: Record<string, LucideIcon>;
  setOpen: (open: boolean) => void;
  /** Where focus goes when the opener is gone, e.g. a closed navigation sheet. */
  fallbackFocus?: React.RefObject<HTMLElement | null>;
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
    queryKey: queryKeys.users,
    queryFn: api.users.list,
    enabled: open && viewer.admin && !viewer.serviceAdmin,
  });
  const { data: modelData } = useQuery({
    queryKey: queryKeys.serviceRegistry,
    queryFn: api.services.registry,
    select: (registry) => {
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
          // "Go to" opened from the navigation sheet: that button has left the
          // page, so focus returns to the control that opened the sheet.
          const opener = commandInvoker.current;
          (opener?.isConnected && opener !== document.body
            ? opener
            : fallbackFocus?.current
          )?.focus();
        }}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>Go to</DialogTitle>
          <DialogDescription>
            Find pages, models, apps and people, or run an action.
          </DialogDescription>
        </DialogHeader>
        <Command>
          <CommandInput
            placeholder="A page, app, model or person…"
            aria-label="Go to"
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
              {/* A person's own settings sit under the account menu, not
                  in the sidebar; the palette finds them too. */}
              {!viewer.serviceAdmin && (
                <CommandItem
                  keywords={["prompts", "privacy", "account"]}
                  onSelect={() => go("/account")}
                >
                  <UserRoundCog />
                  Your settings
                </CommandItem>
              )}
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
