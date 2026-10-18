"use client";

import Link from "next/link";
import { useViewer } from "@/components/shell/session";
import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2, Puzzle } from "lucide-react";
import { toast } from "@/lib/toast";
import {
  adminFetch,
  ApiError,
  adminRegistryMutation,
  api,
} from "@/lib/api";
import { PageHeader } from "@/components/product/page-header";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { ReceiptsSection } from "./receipts";
import { HouseholdPromptsSection } from "./household-prompts";
import { SignInSection } from "./sign-in/sign-in-section";
import { ErrorState } from "@/components/product/error-state";
import { EmptyState } from "@/components/product/empty-state";
import { RelativeTime } from "@/components/product/relative-time";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface Instance {
  name: string;
  base_url: string;
  theme_default: string;
}
interface MediaAdmission {
  disabled_intents: string[];
  disabled_resource_classes: string[];
  reserved_tokens: string[];
  max_queue_depth: number | null;
  retry_after_seconds: number | null;
}
interface ExtensionSource {
  type: "local_directory" | "git";
  path?: string;
  url?: string;
  ref?: string;
}
interface Extension {
  id: string;
  version: string;
  enabled: boolean;
  enabled_at: number | null;
}
interface ExtensionReview {
  ok: boolean;
  package: { id: string; version: string };
  errors: string[];
  warnings: string[];
  requires_code_trust: boolean;
  resolved_source: Record<string, unknown>;
  object_counts: Record<string, number>;
}
interface DefaultsResponse {
  defaults: { invite_access_plan: string | null };
  aliases: Record<string, unknown>;
}
const keys = {
  instance: ["settings", "instance"],
  defaults: ["settings", "defaults"],
  media: ["settings", "media"],
  hostCommands: ["settings", "host-commands"],
  extensions: ["settings", "extensions"],
  effective: ["settings", "effective"],
};
const split = (value: string) =>
  value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);

function useDraft<T>(initial: T) {
  const source = JSON.stringify(initial);
  const [state, setState] = useState({ source, draft: initial });
  if (state.source !== source) {
    setState({
      source,
      draft:
        JSON.stringify(state.draft) === state.source ? initial : state.draft,
    });
  }
  const setDraft = (draft: T) => setState((current) => ({ ...current, draft }));
  return [state.draft, setDraft] as const;
}
function SettingsError({
  error,
  message,
  onRetry,
}: {
  error: unknown;
  message: string;
  onRetry: () => void;
}) {
  return error instanceof ApiError && error.status === 403 ? (
    <p role="alert" className="text-muted-foreground text-meta">
      Only the owner can change this.
    </p>
  ) : (
    <div className="[&_button]:min-h-11 [&_summary]:min-h-11">
      <ErrorState
        heading="Couldn’t load this"
        error={error}
        message={message}
        onRetry={onRetry}
      />
    </div>
  );
}

/** Collapsed sections show a summary; expanded sections show only the editor. */
function Section({
  title,
  summary,
  description,
  children,
}: {
  title: string;
  summary?: { label: ReactNode; description?: ReactNode };
  description?: string;
  children: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const editor = useRef<HTMLDivElement>(null);
  const id = title.toLowerCase().replaceAll(" ", "-");
  return (
    <SettingsSection id={id} title={title} description={description}>
      {summary && !editing ? (
        <SettingRow
          label={summary.label}
          description={summary.description}
          control={
            <Button
              ref={editButton}
              variant="outline"
              className="min-h-11"
              aria-label={`Edit ${title}`}
              onClick={() => {
                setEditing(true);
                requestAnimationFrame(() =>
                  editor.current
                    ?.querySelector<HTMLElement>(
                      "input, [role=combobox], button, summary, a",
                    )
                    ?.focus(),
                );
              }}
            >
              Edit
            </Button>
          }
        />
      ) : (
        <div
          ref={editor}
          id={`${id}-editor`}
          className="min-w-0 space-y-5 p-4 [overflow-wrap:anywhere] sm:p-6"
        >
          {children}
          {summary && (
            <Button
              variant="ghost"
              className="min-h-11"
              aria-label={`Close ${title}`}
              onClick={() => {
                setEditing(false);
                requestAnimationFrame(() => editButton.current?.focus());
              }}
            >
              Close
            </Button>
          )}
        </div>
      )}
    </SettingsSection>
  );
}
function Loading({ label = "Loading settings" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-label={label}
      aria-busy="true"
      className="space-y-4"
    >
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-3/4" />
    </div>
  );
}
function Field({
  id,
  label,
  help,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0 space-y-2">
      <label htmlFor={id} className="text-label">
        {label}
      </label>
      {children}
      {help && (
        <p id={`${id}-help`} className="text-muted-foreground text-meta">
          {help}
        </p>
      )}
    </div>
  );
}
function Save({ pending, label, disabled = false }: { pending: boolean; label: string; disabled?: boolean }) {
  return (
    <Button
      type="submit"
      className="h-auto! min-h-11 max-w-full py-2 whitespace-normal!"
      disabled={pending || disabled}
    >
      {pending && <Loader2 className="animate-spin" />}
      {pending ? "Saving…" : label}
    </Button>
  );
}
function Toggle({
  id,
  label,
  help,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  help?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div className="min-w-0 flex-1 basis-40">
        <label htmlFor={id} className="text-label">
          {label}
        </label>
        {help && (
          <p
            id={`${id}-help`}
            className="text-muted-foreground text-meta mt-0.5"
          >
            {help}
          </p>
        )}
      </div>
      <div className="flex min-h-8 items-center max-sm:min-h-11">
        <Switch
          id={id}
          className="min-h-11 min-w-11"
          aria-describedby={help ? `${id}-help` : undefined}
          checked={checked}
          onCheckedChange={onChange}
        />
      </div>
    </div>
  );
}
function MutationError({ error }: { error: unknown }) {
  return error ? (
    <p role="alert" className="text-destructive text-meta">
      <span className="font-medium">Unsaved changes. </span>
      {error instanceof ApiError &&
      error.status === 409 &&
      error.code !== "oidc_unavailable"
        ? "Settings changed elsewhere; reload to see the latest"
        : error instanceof ApiError && error.status === 403
          ? "Only the owner can change this."
          : error instanceof Error
            ? error.message
            : "Changes could not be saved. Try again."}
    </p>
  ) : null;
}

function InstanceSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.instance,
    staleTime: 15_000,
    queryFn: () =>
      adminFetch<{ instance: Instance; revision: string }>(
        "/admin/api/settings/instance",
      ),
  });
  const theme =
    data?.instance.theme_default === "system"
      ? "Follows device theme"
      : ["light", "dark"].includes(data?.instance.theme_default ?? "")
        ? `${data?.instance.theme_default} theme`
        : null;
  return (
    <Section
      title="Instance"
      summary={
        data
          ? {
              label: data.instance.name || "No name set",
              description:
                [data.instance.base_url, theme].filter(Boolean).join(" · ") ||
                undefined,
            }
          : undefined
      }
    >
      {isPending ? (
        <Loading />
      ) : isError ? (
        <SettingsError
          error={error}
          message="Instance settings could not be loaded."
          onRetry={() => void refetch()}
        />
      ) : (
        <InstanceForm initial={data.instance} revision={data.revision} />
      )}
    </Section>
  );
}
function InstanceForm({
  initial,
  revision,
}: {
  initial: Instance;
  revision: string;
}) {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const [draft, setDraft] = useDraft({ form: initial, revision });
  const { form } = draft;
  const setForm = (form: Instance) => setDraft({ ...draft, form });
  const mutation = useMutation({
    mutationFn: () =>
      adminFetch<{ instance: Instance; revision: string }>(
        "/admin/api/settings/instance",
        {
          method: "PUT",
          body: form,
          headers: { "If-Match": draft.revision },
        },
      ),
    onSuccess: (data) => {
      setDraft({ form: data.instance, revision: data.revision });
      client.setQueryData(keys.instance, data);
      void client.invalidateQueries({ queryKey: ["foundation", "instance"] });
      void client.invalidateQueries({ queryKey: keys.effective });
      toast.success("Instance settings saved");
    },
  });
  if (!owner)
    return (
      <p role="status" className="text-muted-foreground text-meta">
        Only the owner can change this.
      </p>
    );
  return (
    <form
      className="space-y-5"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <Field id="instance-name" label="Instance name">
        <Input
          id="instance-name"
          value={form.name}
          maxLength={100}
          required
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </Field>
      <Field
        id="base-url"
        label="Public base URL"
        help="Use the address people open, without /v1; shared links change, listeners do not."
      >
        <Input
          id="base-url"
          type="url"
          placeholder="https://gateway.example.com"
          aria-describedby="base-url-help"
          value={form.base_url}
          onChange={(e) => setForm({ ...form, base_url: e.target.value })}
        />
      </Field>
      <Field
        id="theme-default"
        label="Default theme"
        help="Used for new visitors. Each person can choose their own theme."
      >
        <Select
          value={form.theme_default}
          onValueChange={(value) => setForm({ ...form, theme_default: value })}
        >
          <SelectTrigger id="theme-default" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="system">Follow device</SelectItem>
            <SelectItem value="light">Light</SelectItem>
            <SelectItem value="dark">Dark</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      <MutationError error={mutation.error} />
      <Save pending={mutation.isPending} label="Save instance" />
    </form>
  );
}
function DefaultsSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.defaults,
    staleTime: 15_000,
    queryFn: async () => {
      const [defaults, plans] = await Promise.all([
        adminFetch<DefaultsResponse>("/admin/api/settings/defaults"),
        api.accessPlans.list(),
      ]);
      return { ...defaults, plans };
    },
  });
  return (
    <Section
      title="People and access"
      description="People can use only what their access plan allows. Nobody has a plan until you choose one."
    >
      {isPending ? (
        <Loading />
      ) : !data ? (
        <SettingsError
          error={error}
          message="Access plans couldn't be loaded. Try again."
          onRetry={() => void refetch()}
        />
      ) : (
        <>
          {isError && (
            <SettingsError
              error={error}
              message="Couldn’t refresh access settings. The values below may be out of date; your edits are kept."
              onRetry={() => void refetch()}
            />
          )}
          <DefaultsForm data={data} />
        </>
      )}
    </Section>
  );
}
const ASK_EACH_TIME = "ask";
function DefaultsForm({
  data,
}: {
  data: DefaultsResponse & {
    plans: { id: string; name: string }[];
  };
}) {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const [plan, setPlan] = useDraft(data.defaults.invite_access_plan ?? ASK_EACH_TIME);
  const planSave = useMutation({
    mutationFn: () =>
      adminFetch("/admin/api/settings/defaults", {
        method: "PUT",
        body: { invite_access_plan: plan === ASK_EACH_TIME ? null : plan },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.defaults });
      void client.invalidateQueries({ queryKey: keys.effective });
    },
  });
  if (!owner)
    return (
      <p role="status" className="text-muted-foreground text-meta">
        Only the owner can change this.
      </p>
    );
  return (
    <div className="space-y-7">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          planSave.mutate();
        }}
      >
        <Field
          id="invite-plan"
          label="Access plan for new invitations"
          help="Preselected when you invite someone. It changes future invitations only."
        >
          <Select
            value={plan}
            disabled={planSave.isPending}
            onValueChange={(value) => {
              setPlan(value);
              planSave.reset();
            }}
          >
            <SelectTrigger
              id="invite-plan"
              aria-describedby="invite-plan-help"
              className="h-auto! min-h-11 w-full *:data-[slot=select-value]:line-clamp-none *:data-[slot=select-value]:min-w-0 *:data-[slot=select-value]:whitespace-normal"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="[&_[role=option]]:min-h-11 [&_[role=option]]:whitespace-normal">
              <SelectGroup>
                <SelectItem value={ASK_EACH_TIME}>Ask each time</SelectItem>
                {data.plans.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Link prefetch={false}
          href="/people?tab=plans"
          className="text-link inline-flex min-h-11 max-w-full items-center underline underline-offset-4"
        >
          View and edit access plans
        </Link>
        <MutationError error={planSave.error} />
        <div className="flex flex-wrap items-center gap-3">
          <Save pending={planSave.isPending} label="Save" />
          <p role="status" className="text-muted-foreground text-meta">
            {planSave.isPending
              ? "Saving…"
              : planSave.isSuccess
                ? "Access plan for new invitations saved"
                : null}
          </p>
        </div>
      </form>

      <details>
        <summary className="text-meta flex min-h-11 cursor-pointer items-center font-medium">
          Default model aliases
        </summary>
        <p className="text-muted-foreground text-meta mb-3">
          Aliases come from bootstrap configuration. Edit the source file and
          restart to change them.
        </p>
        {Object.keys(data.aliases).length ? (
          <pre
            tabIndex={0}
            aria-label="Default model aliases JSON"
            className="bg-muted focus-visible:outline-ring rounded-control overflow-x-auto p-4 font-mono text-xs leading-relaxed focus-visible:outline-2"
          >
            <code>{JSON.stringify(data.aliases, null, 2)}</code>
          </pre>
        ) : (
          <p className="text-muted-foreground text-meta">
            No default aliases configured. Add model aliases in the gateway
            configuration.
          </p>
        )}
      </details>
    </div>
  );
}
function MediaSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.media,
    staleTime: 15_000,
    queryFn: () =>
      adminFetch<{
        overrides: {
          media_job_admission: Partial<MediaAdmission> | null;
          revisions: { media_job_admission_overrides: string };
        };
      }>("/admin/api/operator/overrides"),
  });
  return (
    <Section
      title="Media admission"
      summary={
        data
          ? {
              label:
                data.overrides.media_job_admission?.max_queue_depth ===
                  undefined ||
                data.overrides.media_job_admission?.max_queue_depth === null
                  ? "Queue depth follows runtime default"
                  : `${data.overrides.media_job_admission.max_queue_depth} jobs can queue`,
              description:
                [
                  data.overrides.media_job_admission?.disabled_intents?.length
                    ? `${data.overrides.media_job_admission.disabled_intents.length} paused job types`
                    : null,
                  data.overrides.media_job_admission?.disabled_resource_classes
                    ?.length
                    ? `${data.overrides.media_job_admission.disabled_resource_classes.length} paused resource classes`
                    : null,
                  data.overrides.media_job_admission?.reserved_tokens?.length
                    ? `${data.overrides.media_job_admission.reserved_tokens.length} reserved tokens`
                    : null,
                ]
                  .filter(Boolean)
                  .join(" · ") || "No media admission rules set",
            }
          : undefined
      }
    >
      {isPending ? (
        <Loading />
      ) : isError ? (
        <SettingsError
          error={error}
          message="Media admission settings could not be loaded."
          onRetry={() => void refetch()}
        />
      ) : (
        <MediaForm
          initial={data.overrides.media_job_admission ?? {}}
          revision={data.overrides.revisions.media_job_admission_overrides}
        />
      )}
    </Section>
  );
}
function HostCommandsSection() {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.hostCommands,
    staleTime: 15_000,
    queryFn: () =>
      adminFetch<{ enabled: boolean; revision: string }>(
        "/admin/api/operator/host-commands",
      ),
  });
  return (
    <Section
      title="Computer commands"
      summary={data ? { label: data.enabled ? "Allowed" : "Off" } : undefined}
      description="Controls whether Azizam can start processes or run commands declared for engines."
    >
      {isPending ? (
        <Loading label="Loading computer command setting" />
      ) : isError ? (
        <SettingsError
          error={error}
          message="Computer command setting could not be loaded."
          onRetry={() => void refetch()}
        />
      ) : owner ? (
        <HostCommandsForm initial={data} onSaved={(saved) => {
          client.setQueryData(keys.hostCommands, saved);
          toast.success("Computer command setting saved");
        }} />
      ) : (
        <p className="text-muted-foreground text-meta">Only the owner can change this.</p>
      )}
    </Section>
  );
}

function HostCommandsForm({
  initial,
  onSaved,
}: {
  initial: { enabled: boolean; revision: string };
  onSaved: (saved: { enabled: boolean; revision: string }) => void;
}) {
  const [draft, setDraft] = useDraft(initial);
  const mutation = useMutation({
    mutationFn: () =>
      adminFetch<{ enabled: boolean; revision: string }>(
        "/admin/api/operator/host-commands",
        {
          method: "PUT",
          body: { enabled: draft.enabled },
          headers: { "If-Match": draft.revision },
        },
      ),
    onSuccess: (saved) => {
      setDraft(saved);
      onSaved(saved);
    },
  });
  return (
    <form className="space-y-5" onSubmit={(event) => {
      event.preventDefault();
      mutation.mutate();
    }}>
      <Toggle
        id="allow-host-commands"
        label="Allow Azizam to run commands on this computer"
        help="Engine start, stop and status recipes run as the gateway user."
        checked={draft.enabled}
        onChange={(enabled) => setDraft({ ...draft, enabled })}
      />
      {draft.enabled !== initial.enabled && (
        <p className="text-muted-foreground text-meta" role="status">
          {draft.enabled
            ? "After you save, configured engines may start processes and run commands on this computer."
            : "After you save, Azizam will block new process starts and external manager commands."}
        </p>
      )}
      <MutationError error={mutation.error} />
      <Save pending={mutation.isPending} disabled={draft.enabled === initial.enabled} label="Save computer command setting" />
    </form>
  );
}
function mediaFields(initial: Partial<MediaAdmission>) {
  return {
    disabled_intents: (initial.disabled_intents ?? []).join(", "),
    disabled_resource_classes: (initial.disabled_resource_classes ?? []).join(
      ", ",
    ),
    reserved_tokens: (initial.reserved_tokens ?? []).join(", "),
    max_queue_depth: initial.max_queue_depth?.toString() ?? "",
    retry_after_seconds: initial.retry_after_seconds?.toString() ?? "",
  };
}
function MediaForm({
  initial,
  revision,
}: {
  initial: Partial<MediaAdmission>;
  revision: string;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useDraft({ form: mediaFields(initial), revision });
  const { form } = draft;
  const setForm = (form: ReturnType<typeof mediaFields>) =>
    setDraft({ ...draft, form });
  const update = (key: keyof typeof form, value: string) =>
    setForm({ ...form, [key]: value });
  const mutation = useMutation({
    mutationFn: () =>
      adminFetch<{ media_job_admission: MediaAdmission; revision: string }>(
        "/admin/api/operator/media-job-admission",
        {
          method: "PUT",
          headers: { "If-Match": draft.revision },
          body: {
            disabled_intents: split(form.disabled_intents),
            disabled_resource_classes: split(form.disabled_resource_classes),
            reserved_tokens: split(form.reserved_tokens),
            max_queue_depth:
              form.max_queue_depth === "" ? null : Number(form.max_queue_depth),
            retry_after_seconds:
              form.retry_after_seconds === ""
                ? null
                : Number(form.retry_after_seconds),
          },
        },
      ),
    onSuccess: (data) => {
      setDraft({
        form: mediaFields(data.media_job_admission),
        revision: data.revision,
      });
      client.setQueryData(keys.media, {
        overrides: {
          media_job_admission: data.media_job_admission,
          revisions: { media_job_admission_overrides: data.revision },
        },
      });
      void client.invalidateQueries({ queryKey: keys.media });
      void client.invalidateQueries({ queryKey: keys.effective });
      toast.success("Media admission saved");
    },
  });
  return (
    <form
      className="space-y-5"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          id="queue-depth"
          label="Maximum queued jobs"
          help="Leave blank to use the runtime default."
        >
          <Input
            id="queue-depth"
            type="number"
            min={0}
            step={1}
            value={form.max_queue_depth}
            onChange={(e) => update("max_queue_depth", e.target.value)}
          />
        </Field>
        <Field id="retry-seconds" label="Retry delay (seconds)">
          <Input
            id="retry-seconds"
            type="number"
            min={0}
            step={1}
            value={form.retry_after_seconds}
            onChange={(e) => update("retry_after_seconds", e.target.value)}
          />
        </Field>
      </div>
      <details>
        <summary className="text-meta flex min-h-11 cursor-pointer items-center font-medium">
          Admission rules
        </summary>
        <div className="space-y-5 py-3">
          {(
            [
              ["disabled_intents", "Paused job types"],
              ["disabled_resource_classes", "Paused resource classes"],
              ["reserved_tokens", "Reserved resource tokens"],
            ] as const
          ).map(([key, label]) => (
            <Field
              key={key}
              id={`media-${key}`}
              label={label}
              help="Separate values with commas. Leave blank to clear this rule."
            >
              <Input
                id={`media-${key}`}
                value={form[key]}
                onChange={(e) => update(key, e.target.value)}
              />
            </Field>
          ))}
        </div>
      </details>
      <MutationError error={mutation.error} />
      <Save pending={mutation.isPending} label="Save media admission" />
    </form>
  );
}
function ExtensionsSection() {
  const client = useQueryClient();
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.extensions,
    staleTime: 15_000,
    queryFn: () =>
      adminFetch<{
        extension_packages: { packages: Record<string, Extension> };
      }>("/admin/api/extensions"),
  });
  const [adding, setAdding] = useState(false);
  const [source, setSource] = useState<ExtensionSource>({
    type: "local_directory",
    path: "",
  });
  const [reviewed, setReviewed] = useState<{
    source: ExtensionSource;
    review: ExtensionReview;
  } | null>(null);
  const [trusted, setTrusted] = useState(false);
  const validate = useMutation({
    mutationFn: () =>
      adminFetch<{ review: ExtensionReview }>(
        "/admin/api/extensions/validate",
        { method: "POST", body: { source } },
      ),
    onSuccess: (data) => {
      setReviewed({ source: { ...source }, review: data.review });
      setTrusted(false);
    },
  });
  const enable = useMutation({
    mutationFn: () =>
      adminRegistryMutation("/admin/api/extensions/enable", {
        method: "POST",
        body: {
          source: reviewed!.source,
          review: { resolved_source: reviewed!.review.resolved_source },
        },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.extensions });
      void client.invalidateQueries({ queryKey: keys.effective });
      setAdding(false);
      setReviewed(null);
      toast.success("Extension enabled");
    },
  });
  const editSource = (value: ExtensionSource) => {
    setSource(value);
    setReviewed(null);
    validate.reset();
    enable.reset();
  };
  const packages = Object.values(data?.extension_packages.packages ?? {});
  return (
    <Section
      title="Extensions"
      summary={
        data
          ? {
              label: packages.length
                ? `${packages.filter((pkg) => pkg.enabled).length} enabled extensions`
                : "No extensions installed",
              description: packages.length
                ? packages
                    .slice(0, 3)
                    .map((pkg) => pkg.id)
                    .join(" · ")
                : undefined,
            }
          : undefined
      }
    >
      {isPending ? (
        <Loading />
      ) : isError ? (
        <SettingsError
          error={error}
          message="Extensions could not be loaded."
          onRetry={() => void refetch()}
        />
      ) : (
        <>
          {packages.length ? (
            <ul className="divide-y">
              {packages.map((pkg) => (
                <li
                  key={pkg.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-4"
                >
                  <div className="min-w-0">
                    <p className="text-body font-medium break-words">
                      {pkg.id}
                    </p>
                    <p className="text-muted-foreground text-meta mt-0.5">
                      Version {pkg.version}
                      {pkg.enabled_at ? (
                        <>
                          {" "}
                          · Enabled <RelativeTime ts={pkg.enabled_at} />
                        </>
                      ) : (
                        ""
                      )}
                    </p>
                  </div>
                  <Badge variant="secondary">
                    {pkg.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              icon={Puzzle}
              title="No extensions installed"
              description="Validate a trusted package to add capabilities."
            />
          )}
          <Button variant="outline" onClick={() => setAdding(!adding)}>
            {adding ? "Cancel" : "Add extension"}
          </Button>
          {adding && (
            <form
              className="mt-6 space-y-5"
              onSubmit={(e) => {
                e.preventDefault();
                validate.mutate();
              }}
            >
              <Field id="extension-type" label="Package source">
                <Select
                  value={source.type}
                  onValueChange={(value: "local_directory" | "git") =>
                    editSource(
                      value === "git"
                        ? { type: value, url: "", ref: "" }
                        : { type: value, path: "" },
                    )
                  }
                >
                  <SelectTrigger id="extension-type" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="local_directory">
                      Local directory
                    </SelectItem>
                    <SelectItem value="git">Git repository</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              {source.type === "git" ? (
                <>
                  <Field id="extension-url" label="Repository URL">
                    <Input
                      id="extension-url"
                      type="url"
                      required
                      value={source.url}
                      onChange={(e) =>
                        editSource({ ...source, url: e.target.value })
                      }
                    />
                  </Field>
                  <Field id="extension-ref" label="Branch, tag or commit">
                    <Input
                      id="extension-ref"
                      value={source.ref}
                      onChange={(e) =>
                        editSource({ ...source, ref: e.target.value })
                      }
                    />
                  </Field>
                </>
              ) : (
                <Field id="extension-path" label="Directory on the gateway">
                  <Input
                    id="extension-path"
                    required
                    value={source.path}
                    onChange={(e) =>
                      editSource({ ...source, path: e.target.value })
                    }
                  />
                </Field>
              )}
              <MutationError error={validate.error} />
              <Button
                type="submit"
                variant="outline"
                disabled={validate.isPending || enable.isPending}
              >
                {validate.isPending ? "Validating…" : "Validate package"}
              </Button>
              {reviewed && (
                <div className="space-y-4 border-t pt-5">
                  <p className="text-body font-medium">
                    {reviewed.review.package.id} ·{" "}
                    {reviewed.review.package.version}
                  </p>
                  <p className="text-muted-foreground text-meta">
                    {Object.entries(reviewed.review.object_counts)
                      .map(
                        ([kind, count]) =>
                          `${count} ${kind.replaceAll("_", " ")}`,
                      )
                      .join(" · ")}
                  </p>
                  {reviewed.review.errors.map((error) => (
                    <p
                      key={error}
                      role="alert"
                      className="text-destructive text-meta"
                    >
                      {error}
                    </p>
                  ))}
                  {reviewed.review.warnings.map((warning) => (
                    <p key={warning} className="text-warning text-meta">
                      {warning}
                    </p>
                  ))}
                  {reviewed.review.requires_code_trust && (
                    <Toggle
                      id="extension-trust"
                      label="I trust this package to run code on the gateway"
                      checked={trusted}
                      onChange={setTrusted}
                    />
                  )}
                  <MutationError error={enable.error} />
                  <Button
                    type="button"
                    disabled={
                      !reviewed.review.ok ||
                      reviewed.review.errors.length > 0 ||
                      (reviewed.review.requires_code_trust && !trusted) ||
                      enable.isPending
                    }
                    onClick={() => enable.mutate()}
                  >
                    {enable.isPending ? "Enabling…" : "Enable extension"}
                  </Button>
                </div>
              )}
            </form>
          )}
        </>
      )}
    </Section>
  );
}
function AdvancedSection() {
  const [open, setOpen] = useState(false);
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.effective,
    staleTime: 15_000,
    queryFn: () =>
      adminFetch<{ config: Record<string, unknown>; export_url: string }>(
        "/admin/api/settings/effective",
      ),
    enabled: open,
  });
  return (
    <Section
      title="Advanced"
      summary={{
        label: "Redacted configuration snapshot",
        description: "Read-only export for inspection.",
      }}
    >
      <details onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary className="text-meta flex min-h-11 cursor-pointer items-center font-medium">
          Effective configuration and export
        </summary>
        <div className="space-y-4 pt-3">
          <p className="text-muted-foreground text-meta">
            This is a read-only snapshot, not a restore bundle. Bootstrap
            settings change in their source files; dashboard settings change in
            their own sections.
          </p>
          {open &&
            (isPending ? (
              <Loading />
            ) : isError ? (
              <SettingsError
                error={error}
                message="Effective configuration could not be loaded."
                onRetry={() => void refetch()}
              />
            ) : (
              <>
                <Button variant="outline" asChild>
                  <a href={data.export_url} download>
                    <Download />
                    Export redacted snapshot
                  </a>
                </Button>
                <pre
                  tabIndex={0}
                  aria-label="Effective configuration JSON"
                  className="bg-muted focus-visible:outline-ring rounded-control overflow-x-auto p-4 font-mono text-xs leading-relaxed focus-visible:outline-2"
                >
                  <code>{JSON.stringify(data.config, null, 2)}</code>
                </pre>
              </>
            ))}
        </div>
      </details>
    </Section>
  );
}
export default function SettingsPage() {
  // The settings template: a 720 column, sections 32 apart, and no jump
  // links while every section fits on the page (ban 22).
  return (
    <div className="page-read">
      <PageHeader title="Settings" />
      <div className="page-stack">
        <InstanceSection />
        <SignInSection />
        <DefaultsSection />
        <HouseholdPromptsSection />
        <ReceiptsSection />
        <MediaSection />
        <HostCommandsSection />
        <ExtensionsSection />
        <AdvancedSection />
      </div>
    </div>
  );
}
