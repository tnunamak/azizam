"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Plus, Users, Mail, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { api, adminFetch, ApiError, type Profile } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
import { formatPersonName } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import {
  PageHeader,
  EmptyState,
  ErrorState,
  RelativeTime,
  CopyField,
} from "@/components/product";
import { StatusLine } from "@/components/product/status-ui";
import {
  peopleApi,
  planSummary,
  planCounts,
  planSourceLabel,
  grantSourceLabel,
  type Person,
  type Invite,
} from "./people-api";
import { PlanEditor, TestAccess } from "./plans";

const peopleKey = ["people", "list"];
const invitesKey = ["people", "invites"];
const plansKey = ["profiles"];
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The change could not be saved. Try again.";

function PlanSelect({
  profiles,
  value,
  onChange,
}: {
  profiles: Profile[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger
        id="access-plan"
        className="w-full"
        aria-label="Access plan"
      >
        <SelectValue placeholder="Choose an access plan" />
      </SelectTrigger>
      <SelectContent>
        {profiles.map((p) => (
          <SelectItem key={p.id} value={p.id}>
            {p.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
function Loading({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-label={label}
      aria-busy="true"
      className="space-y-4 py-6"
    >
      {[1, 2, 3].map((v) => (
        <Skeleton key={v} className="h-16 w-full" />
      ))}
    </div>
  );
}

export default function PeoplePage() {
  const client = useQueryClient();
  const owner = useViewer().role === "owner";
  const container = useRef<HTMLDivElement>(null);
  const invoker = useRef<HTMLElement | null>(null);
  const restoreFocus = (event: Event) => {
    event.preventDefault();
    (invoker.current?.isConnected
      ? invoker.current
      : container.current
    )?.focus();
  };
  const {
    data: instanceData,
    isPending: instanceIsPending,
    isError: instanceIsError,
    error: instanceError,
    refetch: instanceRefetch,
  } = useQuery({
    queryKey: ["settings", "instance"],
    queryFn: () =>
      adminFetch<{ instance: { base_url: string } }>(
        "/admin/api/settings/instance",
      ),
    enabled: owner,
  });
  const {
    data: peopleData,
    isPending: peopleIsPending,
    isError: peopleIsError,
    error: peopleError,
    refetch: peopleRefetch,
  } = useQuery({ queryKey: peopleKey, queryFn: peopleApi.people });
  const {
    data: invitesData,
    isPending: invitesIsPending,
    isError: invitesIsError,
    error: invitesError,
    refetch: invitesRefetch,
  } = useQuery({
    queryKey: invitesKey,
    queryFn: peopleApi.invites,
  });
  const {
    data: plansData,
    isPending: plansIsPending,
    isError: plansIsError,
    isFetching: plansIsFetching,
    refetch: plansRefetch,
  } = useQuery({ queryKey: plansKey, queryFn: api.profiles.list });
  const [tab, setTab] = useState("people");
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested && ["people", "invites", "plans"].includes(requested))
      setTab(requested);
  }, []);
  // The command palette links to /people?person=<id>: show that person's row.
  const [requestedPerson] = useState(() =>
    typeof window === "undefined"
      ? null
      : new URLSearchParams(window.location.search).get("person"),
  );
  const requestedFound = Boolean(
    requestedPerson &&
    peopleData?.people.some((person) => person.id === requestedPerson),
  );
  useEffect(() => {
    if (requestedFound) setTab("people");
  }, [requestedFound]);
  // Focus the row once, when it first mounts (the tab content mounts late).
  const requestedShown = useRef(false);
  const showRequestedRow = (node: HTMLDivElement | null) => {
    if (!node || requestedShown.current) return;
    requestedShown.current = true;
    node.scrollIntoView?.({ block: "center" });
    node.focus();
  };
  const [inviteOpen, setInviteOpen] = useState(false);
  const [personPlan, setPersonPlan] = useState<Person | null>(null);
  const [confirmation, setConfirmation] = useState<{
    title: string;
    confirmLabel: string;
    name?: string;
    description: string;
    run: () => Promise<unknown>;
  } | null>(null);
  const [link, setLink] = useState<{
    url: string;
    expires_at: number;
    title: string;
  } | null>(null);
  const linkNeedsPublicBase = link && !/^https?:\/\//i.test(link.url);
  const [editor, setEditor] = useState<Profile | "new" | null>(null);
  const [testing, setTesting] = useState<Profile | null>(null);
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: peopleKey }),
      client.invalidateQueries({ queryKey: invitesKey }),
      client.invalidateQueries({ queryKey: plansKey }),
    ]);
  };
  const reset = useMutation({
    mutationFn: peopleApi.reset,
    onSuccess: (data) => setLink({ ...data, title: "Password reset link" }),
    onError: (error) => toast.error(message(error)),
  });
  const copyInvite = useMutation({
    mutationFn: async (invite: Invite) => ({
      ...(await peopleApi.inviteLink(invite.id)),
      expires_at: invite.expires_at,
    }),
    onSuccess: (data) => setLink({ ...data, title: "Invitation link" }),
    onError: (error) => toast.error(message(error)),
  });
  const profiles =
    plansData || peopleData?.profiles || invitesData?.profiles || [];
  const invites = invitesData?.invites || [];
  // Why "Invite someone" cannot be used yet: an invitation needs an access plan.
  const planGate: "ready" | "loading" | "failed" | "none" = profiles.length
    ? "ready"
    : plansIsPending
      ? "loading"
      : plansIsError
        ? "failed"
        : "none";
  const createPlan = () => {
    setTab("plans");
    setEditor("new");
  };
  const gateCopy = {
    loading: {
      title: "Loading access plans",
      description: "You can invite someone once your access plans load.",
    },
    failed: {
      title: "Access plans couldn’t be loaded",
      description: "Inviting someone needs an access plan. Try again.",
    },
    none: {
      title: "Create an access plan before inviting someone",
      description: "An invitation gives one person the access a plan allows.",
    },
  } as const;
  const gateButton = (size: "sm" | "default") =>
    planGate === "failed" ? (
      <Button
        variant="outline"
        size={size}
        disabled={plansIsFetching}
        onClick={() => void plansRefetch()}
      >
        {plansIsFetching ? "Trying again…" : "Try again"}
      </Button>
    ) : planGate === "none" ? (
      <Button
        variant={size === "sm" ? "outline" : "default"}
        size={size}
        onClick={createPlan}
      >
        Create an access plan
      </Button>
    ) : null;
  // An owner who cannot invite yet gets the reason and a way forward in the
  // empty state of the tab they are on, or beside the Invite action.
  const gateInEmptyState =
    owner &&
    planGate !== "ready" &&
    ((tab === "people" && !!peopleData && !peopleData.people.length) ||
      (tab === "invites" && !!invitesData && !invites.length));
  const actions = (person: Person) =>
    !owner || (person.protected && person.builtin === false) ? null : (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Actions for ${formatPersonName(person.name, "person")}`}
          >
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {!person.protected && (
            <DropdownMenuItem onSelect={() => setPersonPlan(person)}>
              Change access plan
            </DropdownMenuItem>
          )}
          {person.builtin !== false && (
            <DropdownMenuItem
              disabled={reset.isPending}
              onSelect={() => reset.mutate(person.id)}
            >
              Create password reset link
            </DropdownMenuItem>
          )}
          {!person.protected && (
            <>
              <DropdownMenuItem
                onSelect={() =>
                  setConfirmation({
                    title: person.disabled
                      ? "Enable access?"
                      : "Disable access?",
                    confirmLabel: person.disabled
                      ? "Enable access"
                      : "Disable access",
                    description:
                      `${formatPersonName(person.name, person.email || "this person")}${person.name && person.email ? ` (${person.email})` : ""}: ` +
                      (person.disabled
                        ? "This person can sign in again. Previously revoked keys stay revoked; create new keys for their apps."
                        : "This person cannot sign in. Access to their apps is suspended and their keys are permanently revoked."),
                    run: () =>
                      peopleApi.change(person.id, {
                        disabled: !person.disabled,
                      }),
                  })
                }
              >
                {person.disabled ? "Enable access" : "Disable access"}
              </DropdownMenuItem>
            </>
          )}
          {!person.protected && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                onSelect={() =>
                  setConfirmation({
                    title: "Remove person?",
                    confirmLabel: "Remove person",
                    name: formatPersonName(person.name, "person"),
                    description:
                      "This permanently removes the account and revokes access to its apps and keys.",
                    run: () => peopleApi.remove(person.id),
                  })
                }
              >
                Remove person
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  const hasUsageMeasurement = (peopleData?.people ?? []).some(
    (person) => person.requests_this_month != null,
  );
  return (
    <div
      ref={container}
      tabIndex={-1}
      aria-label="People workspace"
      onFocusCapture={(event) => {
        if (
          event.target instanceof HTMLButtonElement &&
          !event.target.closest('[role="dialog"]')
        )
          invoker.current = event.target;
      }}
    >
      <PageHeader
        title="People"
        primaryAction={
          <Button
            onClick={() => setInviteOpen(true)}
            disabled={!owner || !profiles.length}
          >
            <Plus />
            Invite someone
          </Button>
        }
      >
        {owner && planGate !== "ready" && !gateInEmptyState ? (
          <StatusLine
            compact
            tone={
              planGate === "loading"
                ? "unknown"
                : planGate === "failed"
                  ? "bad"
                  : "warn"
            }
            text={gateCopy[planGate].title}
          >
            {gateButton("sm")}
          </StatusLine>
        ) : null}
      </PageHeader>
      <Tabs value={tab} onValueChange={setTab} className="gap-6">
        <TabsList variant="line" className="w-full justify-start border-b">
          <TabsTrigger className="flex-none px-4" value="people">
            People
          </TabsTrigger>
          <TabsTrigger className="flex-none px-4" value="invites">
            Invites
          </TabsTrigger>
          <TabsTrigger className="flex-none px-4" value="plans">
            Access plans
          </TabsTrigger>
        </TabsList>
        <TabsContent value="people">
          {peopleIsPending ? (
            <Loading label="Loading people" />
          ) : peopleIsError ? (
            <ErrorState
              heading="Couldn’t load this"
              error={peopleError}
              message={
                peopleError instanceof ApiError && peopleError.status === 403
                  ? "Only the owner can change this."
                  : "People could not be loaded. Try again."
              }
              onRetry={
                peopleError instanceof ApiError && peopleError.status === 403
                  ? undefined
                  : () => void peopleRefetch()
              }
            />
          ) : !peopleData.people.length ? (
            gateInEmptyState ? (
              <EmptyState
                icon={Users}
                {...gateCopy[planGate]}
                action={gateButton("default")}
              />
            ) : (
              <EmptyState
                icon={Users}
                title="Your people will appear here"
                description="Invite someone to list their access and activity."
                action={
                  <Button
                    disabled={!owner || !profiles.length}
                    onClick={() => setInviteOpen(true)}
                  >
                    Invite someone
                  </Button>
                }
              />
            )
          ) : (
            <div className="divide-y">
              <div
                className={`text-muted-foreground hidden gap-4 pb-3 text-xs md:grid ${hasUsageMeasurement ? "grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_44px]" : "grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_44px]"}`}
              >
                <span>Person</span>
                <span>Access plan</span>
                <span>Last active</span>
                {hasUsageMeasurement && <span>This month</span>}
                <span />
              </div>
              {peopleData.people.map((person) => (
                <div
                  key={person.id}
                  {...(person.id === requestedPerson && {
                    ref: showRequestedRow,
                    tabIndex: -1,
                    "aria-current": "true" as const,
                  })}
                  className={`${person.id === requestedPerson ? "bg-muted rounded-md px-2" : ""} grid grid-cols-[minmax(0,1fr)_44px] gap-4 py-4 md:items-center ${hasUsageMeasurement ? "md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_44px]" : "md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_44px]"}`}
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      {formatPersonName(person.name, "") && (
                        <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
                          {formatPersonName(person.name, "")}
                        </span>
                      )}
                      {person.disabled && (
                        <Badge variant="secondary">Disabled</Badge>
                      )}
                      {person.protected && (
                        <Badge variant="secondary">
                          {person.role === "owner"
                            ? "Owner"
                            : person.role === "admin"
                              ? "Administrator"
                              : "Protected account"}
                        </Badge>
                      )}
                      {person.grants?.map((grant, index) => (
                        <Badge
                          key={`${grant.role}-${grant.source}-${index}`}
                          variant="outline"
                        >
                          {grant.role} · {grantSourceLabel(grant.source)}
                        </Badge>
                      ))}
                    </div>
                    {person.email && (
                      <p className="text-muted-foreground text-sm break-all">
                        {person.email}
                      </p>
                    )}
                  </div>
                  <div className="col-start-1 min-w-0 text-sm [overflow-wrap:anywhere] md:col-auto">
                    <span className="text-muted-foreground md:hidden">
                      Access plan ·{" "}
                    </span>
                    {person.plan_name || "Gateway default"}
                    <div className="text-muted-foreground text-xs">
                      {planSourceLabel(person.plan_source)}
                    </div>
                  </div>
                  {person.last_active ? (
                    <div className="text-muted-foreground col-start-1 text-sm md:col-auto">
                      <span className="md:hidden">Last active · </span>
                      <RelativeTime ts={person.last_active} />
                    </div>
                  ) : (
                    <div aria-hidden="true" className="hidden md:block" />
                  )}
                  {hasUsageMeasurement &&
                    (person.requests_this_month == null ? (
                      <div aria-hidden="true" className="hidden md:block" />
                    ) : (
                      <div className="text-muted-foreground col-start-1 text-sm md:col-auto">
                        <span className="md:hidden">This month · </span>
                        {`${person.requests_this_month.toLocaleString()} requests`}
                      </div>
                    ))}
                  <div className="col-start-2 row-start-1 md:col-auto md:row-auto">
                    {actions(person)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </TabsContent>
        <TabsContent value="invites">
          {invitesIsPending ? (
            <Loading label="Loading invites" />
          ) : invitesIsError ? (
            <ErrorState
              heading="Couldn’t load this"
              error={invitesError}
              message={
                invitesError instanceof ApiError && invitesError.status === 403
                  ? "Only the owner can change this."
                  : "Invites could not be loaded. Try again."
              }
              onRetry={
                invitesError instanceof ApiError && invitesError.status === 403
                  ? undefined
                  : () => void invitesRefetch()
              }
            />
          ) : !invites.length ? (
            gateInEmptyState ? (
              <EmptyState
                icon={Mail}
                {...gateCopy[planGate]}
                action={gateButton("default")}
              />
            ) : (
              <EmptyState
                icon={Mail}
                title="No invitations yet"
                description="Create a single-use link for one access plan."
                action={
                  <Button
                    disabled={!owner || !profiles.length}
                    onClick={() => setInviteOpen(true)}
                  >
                    Invite someone
                  </Button>
                }
              />
            )
          ) : (
            <div className="divide-y">
              {invites.map((invite) => (
                <InviteRow
                  key={invite.id}
                  invite={invite}
                  copy={() => copyInvite.mutate(invite)}
                  copying={copyInvite.isPending}
                  owner={owner}
                  revoke={() =>
                    setConfirmation({
                      title: "Revoke invitation?",
                      confirmLabel: "Revoke invitation",
                      description: `Invitation for ${invite.email || "anyone with the link"}: This link will stop working. You can create a new invitation later.`,
                      run: () => peopleApi.revoke(invite.id),
                    })
                  }
                />
              ))}
              <p className="text-muted-foreground pt-4 text-sm">
                Each invitation can be accepted once. Revoke a link to stop it
                from being used.
              </p>
            </div>
          )}
        </TabsContent>
        <TabsContent value="plans">
          <div className="flex flex-wrap justify-between gap-4 pb-6">
            <p className="text-muted-foreground max-w-xl text-sm">
              Access plans control capabilities, models and remote access. Save
              a plan, then test a request before assigning it.
            </p>
            <Button variant="outline" onClick={() => setEditor("new")}>
              <Plus />
              New access plan
            </Button>
          </div>
          {plansIsPending ? (
            <Loading label="Loading access plans" />
          ) : plansIsError ? (
            <ErrorState
              message="Access plans could not be loaded. Try again."
              onRetry={() => void plansRefetch()}
            />
          ) : !plansData.length ? (
            <EmptyState
              icon={ShieldCheck}
              title="Create your first access plan"
              description="Choose access before inviting people."
              action={
                <Button onClick={() => setEditor("new")}>
                  Create an access plan
                </Button>
              }
            />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {plansData.map((plan) => (
                <article
                  key={plan.id}
                  className="bg-card flex min-w-0 flex-col rounded-xl border p-5"
                >
                  <div className="flex items-start justify-between gap-3">
                    <h2 className="min-w-0 text-base font-semibold [overflow-wrap:anywhere]">
                      {plan.name}
                    </h2>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={`Actions for ${plan.name}`}
                        >
                          <MoreHorizontal />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onSelect={() => setEditor(plan)}>
                          Edit access plan
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          variant="destructive"
                          onSelect={() =>
                            setConfirmation({
                              title: "Delete access plan?",
                              confirmLabel: "Delete access plan",
                              name: plan.name,
                              description:
                                "The gateway blocks deletion while this plan is in use. Reassign people, apps and invitations first.",
                              run: () => api.profiles.delete(plan.id),
                            })
                          }
                        >
                          Delete access plan
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                  <p className="text-muted-foreground mt-2 text-sm leading-6 break-words">
                    {planSummary(plan.profile)}
                  </p>
                  {planCounts(plan.profile) && (
                    <p className="text-muted-foreground mt-3 text-xs">
                      {planCounts(plan.profile)}
                    </p>
                  )}
                  {plan.description && (
                    <p className="mt-3 text-sm break-words">
                      {plan.description}
                    </p>
                  )}
                  <div className="mt-auto flex flex-wrap gap-2 pt-6">
                    <Button variant="outline" onClick={() => setEditor(plan)}>
                      Edit plan
                    </Button>
                    <Button variant="ghost" onClick={() => setTesting(plan)}>
                      Test access
                    </Button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
      {inviteOpen && (
        <InviteDialog
          restoreFocus={restoreFocus}
          profiles={profiles}
          close={() => setInviteOpen(false)}
          saved={async (data) => {
            setLink({
              ...data.invite,
              url: data.url,
              title: "Invitation ready",
            });
            setInviteOpen(false);
            await refresh();
          }}
        />
      )}
      {personPlan && (
        <ChangePlan
          restoreFocus={restoreFocus}
          person={personPlan}
          profiles={profiles}
          close={() => setPersonPlan(null)}
          saved={refresh}
        />
      )}
      {confirmation && (
        <Confirmation
          restoreFocus={restoreFocus}
          {...confirmation}
          close={() => setConfirmation(null)}
          saved={refresh}
        />
      )}
      {link && (
        <Dialog open onOpenChange={(open) => !open && setLink(null)}>
          <DialogContent onCloseAutoFocus={restoreFocus}>
            <DialogHeader>
              <DialogTitle>{link.title}</DialogTitle>
              <DialogDescription>
                {link.title === "Password reset link"
                  ? "Copy and share this link privately. It is only available in this step."
                  : "Copy and share this link privately. Each invitation can be accepted once."}
              </DialogDescription>
            </DialogHeader>
            {linkNeedsPublicBase && instanceIsError ? (
              <ErrorState
                error={instanceError}
                message={
                  instanceError instanceof ApiError &&
                  instanceError.status === 403
                    ? "Only the owner can change this."
                    : "The public URL could not be loaded. Try again before copying this link."
                }
                onRetry={
                  instanceError instanceof ApiError &&
                  instanceError.status === 403
                    ? undefined
                    : () => void instanceRefetch()
                }
              />
            ) : (
              <CopyField
                loading={Boolean(linkNeedsPublicBase && instanceIsPending)}
                value={
                  new URL(
                    link.url,
                    instanceData?.instance.base_url || window.location.origin,
                  ).href
                }
                label="Link"
              />
            )}
            <p className="text-muted-foreground text-sm">
              Expires <RelativeTime ts={link.expires_at} />
            </p>
            <DialogFooter>
              <Button onClick={() => setLink(null)}>I’ve saved the link</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {editor && (
        <PlanEditor
          restoreFocus={restoreFocus}
          plan={editor === "new" ? null : editor}
          close={() => setEditor(null)}
          saved={refresh}
        />
      )}
      {testing && (
        <TestAccess
          restoreFocus={restoreFocus}
          plan={testing}
          close={() => setTesting(null)}
        />
      )}
    </div>
  );
}

function InviteRow({
  invite,
  revoke,
  copy,
  copying,
  owner,
}: {
  invite: Invite;
  revoke: () => void;
  copy: () => void;
  copying: boolean;
  owner: boolean;
}) {
  const isPending =
    !invite.used_at &&
    !invite.revoked_at &&
    invite.expires_at * 1000 > Date.now();
  const status = invite.revoked_at
    ? "Revoked"
    : invite.used_at
      ? "Accepted"
      : invite.expires_at * 1000 <= Date.now()
        ? "Expired"
        : "Pending";
  return (
    <div className="flex items-start justify-between gap-4 py-5">
      <div className="min-w-0">
        <p className="font-medium break-all">
          {invite.email || "Anyone with the link"}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Badge variant={isPending ? "default" : "secondary"}>{status}</Badge>
          <p className="text-muted-foreground min-w-0 text-sm [overflow-wrap:anywhere]">
            {invite.plan_name} ·{" "}
            {status === "Accepted" && invite.used_at ? (
              <>
                Accepted <RelativeTime ts={invite.used_at} />
              </>
            ) : status === "Revoked" && invite.revoked_at ? (
              <>
                Revoked <RelativeTime ts={invite.revoked_at} />
              </>
            ) : status === "Expired" ? (
              <>
                Expired <RelativeTime ts={invite.expires_at} />
              </>
            ) : (
              <>
                Expires <RelativeTime ts={invite.expires_at} />
              </>
            )}
          </p>
        </div>
      </div>
      {owner && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Actions for invitation to ${invite.email || "anyone"}`}
            >
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={copy} disabled={copying || !isPending}>
              Copy invite link
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={revoke}
              disabled={!isPending}
            >
              Revoke invitation
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
function InviteDialog({
  profiles,
  close,
  saved,
  restoreFocus,
}: {
  profiles: Profile[];
  restoreFocus: (event: Event) => void;
  close: () => void;
  saved: (data: { invite: Invite; url: string }) => Promise<void>;
}) {
  const [plan, setPlan] = useState("");
  const [email, setEmail] = useState("");
  const [allowAnyone, setAllowAnyone] = useState(false);
  const [expiry, setExpiry] = useState("604800");
  const selectedPlan = profiles.find((profile) => profile.id === plan);
  const mutation = useMutation({
    mutationFn: () =>
      peopleApi.invite({
        profile_id: plan,
        email: email.trim() || undefined,
        expires_in: Number(expiry),
      }),
    onSuccess: saved,
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !mutation.isPending && close()}
    >
      <DialogContent onCloseAutoFocus={restoreFocus}>
        <DialogHeader>
          <DialogTitle>Invite someone</DialogTitle>
          <DialogDescription>
            Choose their access, then share a single-use link privately.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!email.trim() && !allowAnyone) return;
            mutation.mutate();
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="access-plan">Access plan</Label>
            <PlanSelect profiles={profiles} value={plan} onChange={setPlan} />
          </div>
          {selectedPlan && <PlanPreview plan={selectedPlan} />}
          <div className="space-y-2">
            <Label htmlFor="invite-email">Email (optional)</Label>
            <Input
              id="invite-email"
              type="email"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
                setAllowAnyone(false);
              }}
            />
            <p className="text-muted-foreground text-xs">
              For password signup, this limits the address but does not verify
              the mailbox.
            </p>
          </div>
          {!email.trim() && (
            <div className="flex items-start gap-3">
              <Checkbox
                id="allow-anyone"
                checked={allowAnyone}
                onCheckedChange={(checked) => setAllowAnyone(checked === true)}
              />
              <Label htmlFor="allow-anyone" className="text-sm leading-5">
                Allow anyone with the link to create an account and receive an
                API key
              </Label>
            </div>
          )}
          <div className="space-y-2">
            <Label htmlFor="expiry">Link expires after</Label>
            <Select value={expiry} onValueChange={setExpiry}>
              <SelectTrigger id="expiry" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="86400">1 day</SelectItem>
                <SelectItem value="604800">7 days</SelectItem>
                <SelectItem value="2592000">30 days</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {mutation.isError && (
            <p role="alert" className="text-destructive text-sm">
              {message(mutation.error)}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              disabled={mutation.isPending}
              onClick={close}
            >
              Cancel
            </Button>
            <Button
              disabled={
                !plan || (!email.trim() && !allowAnyone) || mutation.isPending
              }
            >
              {mutation.isPending ? "Creating…" : "Create invite link"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
function ChangePlan({
  person,
  profiles,
  close,
  saved,
  restoreFocus,
}: {
  person: Person;
  profiles: Profile[];
  restoreFocus: (event: Event) => void;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [plan, setPlan] = useState(person.profile_id || "");
  const selectedPlan = profiles.find((profile) => profile.id === plan);
  const mutation = useMutation({
    mutationFn: () => peopleApi.change(person.id, { profile_id: plan }),
    onSuccess: async () => {
      await saved();
      close();
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !mutation.isPending && close()}
    >
      <DialogContent onCloseAutoFocus={restoreFocus}>
        <DialogHeader>
          <DialogTitle>Change access plan</DialogTitle>
          <DialogDescription>
            Choose the access for {formatPersonName(person.name, "this person")}
            . Changes apply to their apps and keys.
          </DialogDescription>
        </DialogHeader>
        <PlanSelect profiles={profiles} value={plan} onChange={setPlan} />
        {selectedPlan && <PlanPreview plan={selectedPlan} />}
        {mutation.isError && (
          <p role="alert" className="text-destructive text-sm">
            {message(mutation.error)}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={close} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!plan || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Saving…" : "Save access plan"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
function PlanPreview({ plan }: { plan: Profile }) {
  return (
    <div className="rounded-lg border p-4">
      <p className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">
        {plan.name}
      </p>
      <p className="text-muted-foreground mt-2 text-sm leading-6">
        {planSummary(plan.profile)}
      </p>
      {planCounts(plan.profile) && (
        <p className="text-muted-foreground mt-2 text-xs">
          {planCounts(plan.profile)}
        </p>
      )}
      {plan.description && (
        <p className="mt-2 text-sm break-words">{plan.description}</p>
      )}
    </div>
  );
}
function Confirmation({
  title,
  confirmLabel,
  name,
  description,
  run,
  close,
  saved,
  restoreFocus,
}: {
  title: string;
  confirmLabel: string;
  name?: string;
  description: string;
  run: () => Promise<unknown>;
  restoreFocus: (event: Event) => void;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [typed, setTyped] = useState("");
  const mutation = useMutation({
    mutationFn: run,
    onSuccess: async () => {
      await saved();
      close();
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !mutation.isPending && close()}
    >
      <DialogContent onCloseAutoFocus={restoreFocus}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {name && (
          <div className="space-y-2">
            <Label htmlFor="confirm-name">Type {name} to confirm</Label>
            <Input
              id="confirm-name"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              disabled={mutation.isPending}
              autoComplete="off"
            />
          </div>
        )}
        {mutation.isError && (
          <p role="alert" className="text-destructive text-sm">
            {message(mutation.error)}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={close} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={mutation.isPending || (!!name && typed !== name)}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Saving…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
