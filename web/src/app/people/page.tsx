"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Plus, Users, Mail, ShieldCheck } from "lucide-react";
import { toast } from "@/lib/toast";
import { api, adminFetch, ApiError, type AccessPlan, type AccessPlanChoice } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
import { formatPersonName } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { SkeletonRows } from "@/components/ui/skeleton";
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
import { StateBadge, StatusLine } from "@/components/product/status-ui";
import {
  ListCard,
  ListRow,
  listFactClass,
} from "@/components/product/list-card";
import { PersonMark } from "@/components/product/person-mark";
import {
  peopleApi,
  grantLabel,
  protectedLabel,
  type Person,
  type Invite,
} from "./people-api";
import { CheckAccess, PlanEditor } from "./plans";

const peopleKey = ["people", "list"];
const invitesKey = ["people", "invites"];
const plansKey = ["access-plans"];
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The change could not be saved. Try again.";

function PlanSelect({
  profiles,
  value,
  onChange,
}: {
  profiles: AccessPlanChoice[];
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
      className="bg-card rounded-panel border"
    >
      <SkeletonRows count={3} lines={2} />
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
  } = useQuery({ queryKey: plansKey, queryFn: api.accessPlans.list });
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
  const showRequestedRow = (node: HTMLElement | null) => {
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
  const [editor, setEditor] = useState<AccessPlan | "new" | null>(null);
  const [testing, setTesting] = useState<AccessPlan | null>(null);
  // Check access links to /people?plan=<id>: open that plan's editor once, for the owner.
  const requestedPlan = useRef(
    typeof window === "undefined"
      ? null
      : new URLSearchParams(window.location.search).get("plan"),
  );
  useEffect(() => {
    if (!owner || !requestedPlan.current || !plansData) return;
    const found = plansData.find((item) => item.id === requestedPlan.current);
    requestedPlan.current = null;
    if (!found) return;
    setTab("plans");
    setEditor(found);
  }, [owner, plansData]);
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
  const profiles: AccessPlanChoice[] =
    plansData || peopleData?.access_plans || invitesData?.access_plans || [];
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
                        : "This person cannot sign in. Access to their apps is suspended and their keys are permanently revoked. Their saved prompts stay."),
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
                      "This permanently removes the account and its saved prompts, and revokes access to its apps and keys.",
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
      onPointerDownCapture={(event) => {
        const button =
          event.target instanceof Element
            ? event.target.closest("button")
            : null;
        // Dropdown triggers prevent pointer focus; record the opener before
        // the new dialog's focus trap can intercept menu-close autofocus.
        if (button && !button.closest('[role="dialog"]'))
          invoker.current = button;
      }}
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
        <TabsList
          variant="line"
          className="w-full justify-start gap-4 border-b"
        >
          <TabsTrigger className="flex-none px-1" value="people">
            People
          </TabsTrigger>
          <TabsTrigger className="flex-none px-1" value="invites">
            Invites
          </TabsTrigger>
          <TabsTrigger className="flex-none px-1" value="plans">
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
            // The list template (DESIGN "Page templates"): one ListCard, a
            // 36 header band over 60 rows (64 on a phone). Role and where it
            // came from are words on the second line, not chips; one state
            // badge at most (Disabled).
            <ListCard
              label="People"
              header={
                <>
                  <span className="min-w-0 flex-1 pl-11">Person</span>
                  {/* The fact columns, spaced like ListRow's (16 apart). */}
                  <span className="flex shrink-0 gap-4 max-sm:hidden">
                    <span className={listFactClass}>Access plan</span>
                    <span className={listFactClass}>Last active</span>
                    {hasUsageMeasurement && (
                      <span className={listFactClass}>This month</span>
                    )}
                  </span>
                  <span aria-hidden="true" className="w-8 shrink-0" />
                </>
              }
            >
              {peopleData.people.map((person) => {
                const name = formatPersonName(person.name, "");
                const roles = person.grants?.length
                  ? person.grants.map((grant, index) => (
                      <span key={`${grant.role}-${grant.source}-${index}`}>
                        {grantLabel(grant)}
                      </span>
                    ))
                  : person.protected
                    ? [
                        <span key="protected">
                          {protectedLabel(person.role)}
                        </span>,
                      ]
                    : [];
                return (
                  <ListRow
                    key={person.id}
                    lines={2}
                    phoneLine="facts"
                    {...(person.id === requestedPerson && {
                      ref: showRequestedRow,
                      tabIndex: -1,
                      "aria-current": "true" as const,
                    })}
                    className={
                      person.id === requestedPerson ? "bg-muted" : undefined
                    }
                    leading={
                      <PersonMark
                        name={name || person.email || "?"}
                        size="md"
                      />
                    }
                    title={
                      <span title={name || person.email || undefined}>
                        {name || person.email}
                      </span>
                    }
                    badge={
                      person.disabled ? (
                        <StateBadge state="off" word="Disabled" />
                      ) : !person.plan_id ? (
                        <StateBadge state="off" word="No access plan" />
                      ) : undefined
                    }
                    meta={[
                      name ? person.email : null,
                      roles.length ? (
                        <span key="roles">
                          {roles.map((role, index) => (
                            <span key={index}>
                              {index ? ", " : null}
                              {role}
                            </span>
                          ))}
                        </span>
                      ) : null,
                    ]}
                    facts={[
                      <span
                        key="plan"
                        title={person.plan_name || "No access plan"}
                      >
                        <span>{person.plan_name || "Choose a plan"}</span>
                      </span>,
                      person.last_active ? (
                        <RelativeTime
                          key="last-active"
                          ts={person.last_active}
                        />
                      ) : null,
                      ...(hasUsageMeasurement
                        ? [
                            person.requests_this_month == null ? null : (
                              <span key="month">
                                {/* No header names this on a phone. */}
                                <span className="sm:hidden">This month · </span>
                                {`${person.requests_this_month.toLocaleString()} requests`}
                              </span>
                            ),
                          ]
                        : []),
                    ]}
                    trailing={
                      actions(person) ?? (
                        <span aria-hidden="true" className="w-8" />
                      )
                    }
                  />
                );
              })}
            </ListCard>
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
            <div className="space-y-4">
              <ListCard label="Invitations">
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
              </ListCard>
              <p className="text-muted-foreground text-meta">
                Each invitation can be accepted once. Revoke a link to stop it
                from being used.
              </p>
            </div>
          )}
        </TabsContent>
        <TabsContent value="plans">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
            <p className="text-muted-foreground text-meta max-w-xl">
              Access plans control capabilities, models and remote access. Save
              a plan, then test a request before assigning it.
            </p>
            {owner ? (
              <Button variant="outline" onClick={() => setEditor("new")}>
                <Plus />
                New access plan
              </Button>
            ) : (
              <p className="text-muted-foreground text-meta">
                Only the owner can change access plans.
              </p>
            )}
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
              title={owner ? "Create your first access plan" : "No access plans yet"}
              description={
                owner
                  ? "Choose access before inviting people."
                  : "The owner creates access plans."
              }
              action={
                owner ? (
                  <Button onClick={() => setEditor("new")}>
                    Create an access plan
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <ListCard label="Access plans">
              {plansData.map((plan) => (
                <ListRow
                  key={plan.id}
                  lines={2}
                  title={plan.name}
                  titleAs="h2"
                  // One line: what the plan allows and its counts; with the
                  // description, in full on hover and in the plan editor.
                  meta={[plan.summary, usedByLabel(plan)]}
                  metaTitle={[plan.summary, usedByLabel(plan)]
                    .filter(Boolean)
                    .join(" · ")}
                  trailing={
                    <>
                      {owner && (
                        <Button
                          variant="outline"
                          className="max-sm:hidden"
                          onClick={() => setEditor(plan)}
                        >
                          Edit plan
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        className="max-sm:hidden"
                        onClick={() => setTesting(plan)}
                      >
                        Check access
                      </Button>
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
                          {owner && (
                            <DropdownMenuItem onSelect={() => setEditor(plan)}>
                              Edit access plan
                            </DropdownMenuItem>
                          )}
                          <DropdownMenuItem onSelect={() => setTesting(plan)}>
                            Check access
                          </DropdownMenuItem>
                          {owner && (
                            <>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                variant="destructive"
                                onSelect={() =>
                                  setConfirmation({
                                    title: "Delete access plan?",
                                    confirmLabel: "Delete access plan",
                                    name: plan.name,
                                    description:
                                      "The gateway blocks deletion while this plan is in use. Move people, apps and invitations to another plan first.",
                                    run: () => api.accessPlans.delete(plan.id),
                                  })
                                }
                              >
                                Delete access plan
                              </DropdownMenuItem>
                            </>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </>
                  }
                />
              ))}
            </ListCard>
          )}
        </TabsContent>
      </Tabs>
      {inviteOpen && (
        <InviteDialog
          restoreFocus={restoreFocus}
          profiles={profiles}
          preselected={invitesData?.invite_access_plan ?? null}
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
            <p className="text-muted-foreground text-meta">
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
        <CheckAccess
          restoreFocus={restoreFocus}
          plan={testing}
          close={() => setTesting(null)}
          canEdit={owner}
          onEditPlan={(planId) => {
            const found = plansData?.find((item) => item.id === planId);
            if (!found) return;
            setTesting(null);
            setEditor(found);
          }}
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
    <ListRow
      lines={2}
      title={invite.email || "Anyone with the link"}
      // One state badge, for the invitations that still need someone.
      badge={isPending ? <StateBadge state="busy" word="Pending" /> : undefined}
      meta={[
        invite.plan_name,
        status === "Accepted" && invite.used_at ? (
          <span key="when">
            <span>Accepted</span> <RelativeTime ts={invite.used_at} />
          </span>
        ) : status === "Revoked" && invite.revoked_at ? (
          <span key="when">
            <span>Revoked</span> <RelativeTime ts={invite.revoked_at} />
          </span>
        ) : status === "Expired" ? (
          <span key="when">
            <span>Expired</span> <RelativeTime ts={invite.expires_at} />
          </span>
        ) : (
          <span key="when">
            <span>Expires</span> <RelativeTime ts={invite.expires_at} />
          </span>
        ),
      ]}
      trailing={
        owner ? (
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
              <DropdownMenuItem
                onSelect={copy}
                disabled={copying || !isPending}
              >
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
        ) : undefined
      }
    />
  );
}
function InviteDialog({
  profiles,
  preselected,
  close,
  saved,
  restoreFocus,
}: {
  profiles: AccessPlanChoice[];
  /** The Settings choice for new invitations; null means "Ask each time". */
  preselected: string | null;
  restoreFocus: (event: Event) => void;
  close: () => void;
  saved: (data: { invite: Invite; url: string }) => Promise<void>;
}) {
  const [plan, setPlan] = useState(preselected ?? "");
  const [email, setEmail] = useState("");
  const [allowAnyone, setAllowAnyone] = useState(false);
  const [expiry, setExpiry] = useState("604800");
  const selectedPlan = profiles.find((profile) => profile.id === plan);
  const mutation = useMutation({
    mutationFn: () =>
      peopleApi.invite({
        access_plan: plan,
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
            <p className="text-muted-foreground text-meta">
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
              <Label htmlFor="allow-anyone" className="text-meta">
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
            <p role="alert" className="text-destructive text-meta">
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
  profiles: AccessPlanChoice[];
  restoreFocus: (event: Event) => void;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [plan, setPlan] = useState(person.plan_id || "");
  const selectedPlan = profiles.find((profile) => profile.id === plan);
  const mutation = useMutation({
    mutationFn: () => peopleApi.change(person.id, { access_plan: plan }),
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
          <p role="alert" className="text-destructive text-meta">
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
function PlanPreview({ plan }: { plan: AccessPlanChoice }) {
  return (
    <div className="rounded-panel border p-4">
      <p className="text-body min-w-0 font-medium [overflow-wrap:anywhere]">
        {plan.name}
      </p>
      {plan.summary && (
        <p className="text-muted-foreground text-meta mt-2">{plan.summary}</p>
      )}
    </div>
  );
}

/** "Used by 3 people and 5 apps" (design §3.4). */
function usedByLabel(plan: AccessPlan): string | null {
  const used = plan.used_by;
  if (!used) return null;
  const parts = [
    used.people.length ? `${used.people.length} ${used.people.length === 1 ? "person" : "people"}` : null,
    used.apps.length ? `${used.apps.length} ${used.apps.length === 1 ? "app" : "apps"}` : null,
  ].filter(Boolean);
  return parts.length ? `Used by ${parts.join(" and ")}` : "Not used yet";
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
          <p role="alert" className="text-destructive text-meta">
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
