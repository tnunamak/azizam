"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Plus, Users, Mail, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { api, type Profile } from "@/lib/api";
import { formatPersonName } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
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
import {
  peopleApi,
  planSummary,
  planCounts,
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
  const {
    data: peopleData,
    isPending: peopleIsPending,
    isError: peopleIsError,
    refetch: peopleRefetch,
  } = useQuery({ queryKey: peopleKey, queryFn: peopleApi.people });
  const {
    data: invitesData,
    isPending: invitesIsPending,
    isError: invitesIsError,
    refetch: invitesRefetch,
  } = useQuery({
    queryKey: invitesKey,
    queryFn: peopleApi.invites,
  });
  const {
    data: plansData,
    isPending: plansIsPending,
    isError: plansIsError,
    refetch: plansRefetch,
  } = useQuery({ queryKey: plansKey, queryFn: api.profiles.list });
  const [tab, setTab] = useState("people");
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested && ["people", "invites", "plans"].includes(requested))
      setTab(requested);
  }, []);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [personPlan, setPersonPlan] = useState<Person | null>(null);
  const [confirmation, setConfirmation] = useState<{
    title: string;
    name?: string;
    description: string;
    run: () => Promise<unknown>;
  } | null>(null);
  const [link, setLink] = useState<{
    url: string;
    expires_at: number;
    title: string;
  } | null>(null);
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
  const actions = (person: Person) =>
    person.protected && person.builtin === false ? null : (
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
                    description: person.disabled
                      ? "This person can sign in again. Previously revoked keys stay revoked; create new keys for their apps."
                      : "This person cannot sign in. Their apps are disabled and their keys are permanently revoked.",
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
  return (
    <div>
      <PageHeader
        title="People"
        subtitle="Give someone the right access."
        primaryAction={
          <Button
            onClick={() => setInviteOpen(true)}
            disabled={!profiles.length}
          >
            <Plus />
            Invite someone
          </Button>
        }
      />
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
              message="People could not be loaded. Try again."
              onRetry={() => void peopleRefetch()}
            />
          ) : !peopleData.people.length ? (
            <EmptyState
              icon={Users}
              title="Your people will appear here"
              description="Invite someone to list their access and activity."
              action={
                <Button
                  onClick={() =>
                    profiles.length
                      ? setInviteOpen(true)
                      : (setTab("plans"), setEditor("new"))
                  }
                >
                  {profiles.length ? "Invite someone" : "Create an access plan"}
                </Button>
              }
            />
          ) : (
            <div className="divide-y">
              <div className="text-muted-foreground hidden grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_44px] gap-4 pb-3 text-xs md:grid">
                <span>Person</span>
                <span>Access plan</span>
                <span>Last active</span>
                <span>This month</span>
                <span />
              </div>
              {peopleData.people.map((person) => (
                <div
                  key={person.id}
                  className="grid grid-cols-[minmax(0,1fr)_44px] gap-4 py-4 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_44px] md:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium break-words">
                        {formatPersonName(person.name, "Unnamed person")}
                      </span>
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
                    </div>
                    <p className="text-muted-foreground text-sm break-all">
                      {person.email || "No email provided"}
                    </p>
                  </div>
                  <div className="col-start-1 text-sm md:col-auto">
                    <span className="text-muted-foreground md:hidden">
                      Access plan ·{" "}
                    </span>
                    {person.plan_name || "Gateway default"}
                  </div>
                  <div className="text-muted-foreground col-start-1 text-sm md:col-auto">
                    <span className="md:hidden">Last active · </span>
                    {person.last_active ? (
                      <RelativeTime ts={person.last_active} />
                    ) : (
                      "Not measured yet"
                    )}
                  </div>
                  <div className="text-muted-foreground col-start-1 text-sm md:col-auto">
                    <span className="md:hidden">This month · </span>
                    {person.requests_this_month === null ? (
                      <span className="sr-only">
                        Request count not measured
                      </span>
                    ) : (
                      `${person.requests_this_month.toLocaleString()} requests`
                    )}
                  </div>
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
              message="Invites could not be loaded. Try again."
              onRetry={() => void invitesRefetch()}
            />
          ) : !invites.length ? (
            <EmptyState
              icon={Mail}
              title="No invitations yet"
              description="Create a single-use link for one access plan."
              action={
                <Button
                  disabled={!profiles.length}
                  onClick={() => setInviteOpen(true)}
                >
                  Invite someone
                </Button>
              }
            />
          ) : (
            <div className="divide-y">
              {invites.map((invite) => (
                <InviteRow
                  key={invite.id}
                  invite={invite}
                  copy={() => copyInvite.mutate(invite)}
                  copying={copyInvite.isPending}
                  revoke={() =>
                    setConfirmation({
                      title: "Revoke invitation?",
                      description:
                        "This link will stop working. You can create a new invitation later.",
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
                    <h2 className="text-base font-semibold break-words">
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
          person={personPlan}
          profiles={profiles}
          close={() => setPersonPlan(null)}
          saved={refresh}
        />
      )}
      {confirmation && (
        <Confirmation
          {...confirmation}
          close={() => setConfirmation(null)}
          saved={refresh}
        />
      )}
      {link && (
        <Dialog open onOpenChange={(open) => !open && setLink(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{link.title}</DialogTitle>
              <DialogDescription>
                {link.title === "Password reset link"
                  ? "Copy and share this link privately. It is only available in this step."
                  : "Copy and share this link privately. Each invitation can be accepted once."}
              </DialogDescription>
            </DialogHeader>
            <CopyField
              value={new URL(link.url, window.location.origin).href}
              label="Link"
            />
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
          plan={editor === "new" ? null : editor}
          close={() => setEditor(null)}
          saved={refresh}
        />
      )}
      {testing && <TestAccess plan={testing} close={() => setTesting(null)} />}
    </div>
  );
}

function InviteRow({
  invite,
  revoke,
  copy,
  copying,
}: {
  invite: Invite;
  revoke: () => void;
  copy: () => void;
  copying: boolean;
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
          <p className="text-muted-foreground text-sm">
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
    </div>
  );
}
function InviteDialog({
  profiles,
  close,
  saved,
}: {
  profiles: Profile[];
  close: () => void;
  saved: (data: { invite: Invite; url: string }) => Promise<void>;
}) {
  const [plan, setPlan] = useState("");
  const [email, setEmail] = useState("");
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
      <DialogContent>
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
              onChange={(e) => setEmail(e.target.value)}
            />
            <p className="text-muted-foreground text-xs">
              Leave blank to let anyone with the link accept.
            </p>
          </div>
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
            <Button disabled={!plan || mutation.isPending}>
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
}: {
  person: Person;
  profiles: Profile[];
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
      <DialogContent>
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
      <p className="text-sm font-medium">{plan.name}</p>
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
  name,
  description,
  run,
  close,
  saved,
}: {
  title: string;
  name?: string;
  description: string;
  run: () => Promise<unknown>;
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
      <DialogContent>
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
            {mutation.isPending ? "Saving…" : "Confirm"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
