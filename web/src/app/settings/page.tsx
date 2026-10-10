"use client";

import Link from "next/link";
import { useViewer } from "@/components/shell/session";
import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2, Puzzle } from "lucide-react";
import { toast } from "sonner";
import {
  adminFetch,
  ApiError,
  adminRegistryMutation,
  api,
  type EffectiveOidcConfig,
  type OidcConfigUpdate,
  type UserAccessDefaults,
} from "@/lib/api";
import { PageHeader } from "@/components/product/page-header";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
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
  defaults: { profile_id: string | null };
  aliases: Record<string, unknown>;
}
type SignInMode = "built-in" | "oidc" | "both";
interface SignInOptions {
  revision: string;
  mode: SignInMode;
  built_in: boolean;
  oidc: boolean;
  oidc_label: string;
}
const keys = {
  instance: ["settings", "instance"],
  oidc: ["settings", "oidc"],
  signIn: ["settings", "sign-in"],
  defaults: ["settings", "defaults"],
  accessDefaults: ["settings", "access-defaults"],
  media: ["settings", "media"],
  extensions: ["settings", "extensions"],
  effective: ["settings", "effective"],
};
const split = (value: string) =>
  value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
const LEGACY_ACCESS_PLAN_NAMES = new Set(["legacy simon", "legacy daisy"]);
const isLegacyAccessPlan = (name: string) =>
  LEGACY_ACCESS_PLAN_NAMES.has(name.trim().toLowerCase());

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
    <ErrorState
      heading="Couldn’t load this"
      error={error}
      message={message}
      onRetry={onRetry}
    />
  );
}

/**
 * One settings section (DESIGN "Page templates", settings page): a heading
 * over one card. The card's row says what is set now in one line and one
 * line of detail, with Edit at 32 on the right; Edit opens the form under
 * the row, inside the same card, where the full detail is.
 */
function Section({
  title,
  summary,
  children,
}: {
  title: string;
  summary?: { label: ReactNode; description?: ReactNode };
  children: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <SettingsSection
      id={title.toLowerCase().replaceAll(" ", "-")}
      title={title}
    >
      {summary ? (
        <SettingRow
          label={summary.label}
          description={summary.description}
          control={
            editing ? (
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Close
              </Button>
            ) : (
              <Button
                variant="outline"
                aria-label={`Edit ${title}`}
                onClick={() => setEditing(true)}
              >
                Edit
              </Button>
            )
          }
        />
      ) : null}
      {!summary || editing ? (
        <div className="min-w-0 space-y-5 p-4 [overflow-wrap:anywhere] sm:p-6">
          {children}
        </div>
      ) : null}
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
function Save({ pending, label }: { pending: boolean; label: string }) {
  return (
    <Button type="submit" disabled={pending}>
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
    <div className="flex items-center justify-between gap-4">
      <div>
        <label htmlFor={id} className="text-label">
          {label}
        </label>
        {help && (
          <p className="text-muted-foreground text-meta mt-0.5">{help}</p>
        )}
      </div>
      <div className="flex min-h-8 items-center max-sm:min-h-11">
        <Switch id={id} checked={checked} onCheckedChange={onChange} />
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
function SignInSection() {
  const {
    data: oidcData,
    isPending: oidcIsPending,
    isError: oidcIsError,
    error: oidcError,
    refetch: refetchOidc,
  } = useQuery({
    queryKey: keys.oidc,
    queryFn: () =>
      adminFetch<{ oidc_config: EffectiveOidcConfig | null; revision: string }>(
        "/admin/api/oidc-config",
      ),
  });
  const {
    data: signInData,
    isPending: signInIsPending,
    isError: signInIsError,
    error: signInError,
    refetch: refetchSignIn,
  } = useQuery({
    queryKey: keys.signIn,
    queryFn: () => adminFetch<SignInOptions>("/admin/api/operator/sign-in"),
  });
  return (
    <Section
      title="Sign-in"
      summary={
        oidcData && signInData
          ? {
              label:
                signInData.mode === "both"
                  ? "Built-in accounts and identity provider"
                  : signInData.mode === "oidc"
                    ? "Identity provider only"
                    : "Built-in accounts only",
              description: oidcData.oidc_config?.enabled
                ? `Identity provider enabled${oidcData.oidc_config.issuer ? ` · ${oidcData.oidc_config.issuer}` : ""}`
                : "No identity provider enabled",
            }
          : undefined
      }
    >
      <div className="space-y-7">
        {signInIsPending ? (
          <Loading label="Loading sign-in choices" />
        ) : signInIsError ? (
          <SettingsError
            error={signInError}
            message="Sign-in choices could not be loaded."
            onRetry={() => void refetchSignIn()}
          />
        ) : (
          <SignInChoiceForm signIn={signInData} />
        )}
        {oidcIsPending ? (
          <Loading label="Loading identity provider" />
        ) : oidcIsError ? (
          <SettingsError
            error={oidcError}
            message="Sign-in settings could not be loaded."
            onRetry={() => void refetchOidc()}
          />
        ) : (
          <OidcForm
            config={oidcData.oidc_config}
            revision={oidcData.revision}
          />
        )}
      </div>
    </Section>
  );
}
function SignInChoiceForm({ signIn }: { signIn: SignInOptions }) {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const [draft, setDraft] = useDraft({
    mode: signIn.mode,
    revision: signIn.revision,
  });
  const { mode } = draft;
  const setMode = (mode: SignInMode) => setDraft({ ...draft, mode });
  const modeMutation = useMutation({
    mutationFn: () =>
      adminFetch<SignInOptions>("/admin/api/operator/sign-in", {
        method: "PUT",
        body: { mode },
        headers: { "If-Match": draft.revision },
      }),
    onSuccess: (data) => {
      setDraft({ mode: data.mode, revision: data.revision });
      client.setQueryData(keys.signIn, data);
      void client.invalidateQueries({ queryKey: keys.oidc });
      toast.success("Sign-in choice saved");
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
        modeMutation.mutate();
      }}
    >
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant={signIn.built_in ? "default" : "secondary"}>
          Built-in accounts {signIn.built_in ? "on" : "off"}
        </Badge>
        <Badge variant={signIn.oidc ? "default" : "secondary"}>
          OIDC {signIn.oidc ? "on" : "off"}
        </Badge>
      </div>
      <Field
        id="sign-in-mode"
        label="Available sign-in methods"
        help="This setting controls the methods shown on the sign-in page. The provider switch below only configures OIDC."
      >
        <Select
          value={mode}
          onValueChange={(value) => setMode(value as SignInMode)}
        >
          <SelectTrigger id="sign-in-mode" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="built-in">Built-in accounts only</SelectItem>
            <SelectItem value="oidc">OIDC only</SelectItem>
            <SelectItem value="both">Built-in accounts and OIDC</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      <MutationError error={modeMutation.error} />
      <Save pending={modeMutation.isPending} label="Save sign-in choice" />
    </form>
  );
}
function oidcFields(config: EffectiveOidcConfig | null) {
  return {
    enabled: config?.enabled ?? false,
    issuer: config?.issuer ?? "",
    client_id: config?.client_id ?? "",
    redirect_uri: config?.redirect_uri ?? "",
    client_secret: "",
    scopes: (config?.scopes ?? ["openid", "email", "profile"]).join(", "),
    groups_claim: config?.groups_claim ?? "groups",
    allowed_email_domains: config?.allowed_email_domains?.join(", ") ?? "",
    admin_emails: config?.admin_emails?.join(", ") ?? "",
    admin_groups: config?.admin_groups?.join(", ") ?? "",
    admin_roles: config?.admin_roles?.join(", ") ?? "",
    authorization_endpoint: config?.authorization_endpoint ?? "",
    token_endpoint: config?.token_endpoint ?? "",
    jwks_uri: config?.jwks_uri ?? "",
    clock_skew_seconds: String(config?.clock_skew_seconds ?? 60),
  };
}
function OidcForm({
  config,
  revision,
}: {
  config: EffectiveOidcConfig | null;
  revision: string;
}) {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const [draft, setDraft] = useDraft({ fields: oidcFields(config), revision });
  const { fields } = draft;
  const setFields = (fields: ReturnType<typeof oidcFields>) =>
    setDraft({ ...draft, fields });
  const update = (key: keyof typeof fields, value: string) =>
    setFields({ ...fields, [key]: value });
  const mutation = useMutation({
    mutationFn: () => {
      const body: OidcConfigUpdate = {
        enabled: fields.enabled,
        issuer: fields.issuer.trim(),
        client_id: fields.client_id.trim(),
        redirect_uri: fields.redirect_uri.trim(),
        scopes: split(fields.scopes),
        groups_claim: fields.groups_claim,
        allowed_email_domains: split(fields.allowed_email_domains),
        admin_emails: split(fields.admin_emails),
        admin_groups: split(fields.admin_groups),
        admin_roles: split(fields.admin_roles),
        authorization_endpoint: fields.authorization_endpoint,
        token_endpoint: fields.token_endpoint,
        jwks_uri: fields.jwks_uri,
        clock_skew_seconds: Number(fields.clock_skew_seconds),
      };
      if (fields.client_secret.trim())
        body.client_secret = fields.client_secret;
      return adminFetch<{ oidc_config: EffectiveOidcConfig; revision: string }>(
        "/admin/api/oidc-config",
        { method: "PUT", body, headers: { "If-Match": draft.revision } },
      );
    },
    onSuccess: (data) => {
      setDraft({
        fields: oidcFields(data.oidc_config),
        revision: data.revision,
      });
      client.setQueryData(keys.oidc, data);
      void client.invalidateQueries({ queryKey: keys.signIn });
      void client.invalidateQueries({ queryKey: keys.effective });
      toast.success("Identity provider saved");
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
      className="space-y-5 border-t pt-7"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      {!config && (
        <p className="text-muted-foreground text-meta">
          No identity provider is connected yet. Add its details below to enable
          sign-in.
        </p>
      )}
      <Toggle
        id="oidc-enabled"
        label="Enable identity provider"
        checked={fields.enabled}
        onChange={(enabled) => setFields({ ...fields, enabled })}
      />
      <Field id="oidc-issuer" label="Issuer URL">
        <Input
          id="oidc-issuer"
          type="url"
          required={fields.enabled}
          value={fields.issuer}
          onChange={(e) => update("issuer", e.target.value)}
        />
      </Field>
      <Field id="oidc-client" label="Client ID">
        <Input
          id="oidc-client"
          required={fields.enabled}
          value={fields.client_id}
          onChange={(e) => update("client_id", e.target.value)}
        />
      </Field>
      <Field
        id="oidc-secret"
        label="Client secret"
        help={
          config?.client_secret_set
            ? "A secret is saved. Leave this blank to keep it."
            : "Write-only. The saved value is never shown."
        }
      >
        <Input
          id="oidc-secret"
          type="password"
          autoComplete="new-password"
          value={fields.client_secret}
          onChange={(e) => update("client_secret", e.target.value)}
        />
      </Field>
      <Field id="oidc-redirect" label="Redirect URL">
        <Input
          id="oidc-redirect"
          type="url"
          required={fields.enabled}
          value={fields.redirect_uri}
          onChange={(e) => update("redirect_uri", e.target.value)}
        />
      </Field>
      <details>
        <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
          Provider details and access rules
        </summary>
        <div className="space-y-5 pb-2">
          {(
            [
              ["scopes", "Scopes"],
              ["groups_claim", "Groups claim"],
              ["allowed_email_domains", "Allowed email domains"],
              ["admin_emails", "Sign-in email exceptions"],
              ["admin_groups", "Admin groups"],
              ["admin_roles", "Admin roles"],
              ["authorization_endpoint", "Authorization endpoint"],
              ["token_endpoint", "Token endpoint"],
              ["jwks_uri", "Signing keys URL"],
            ] as const
          ).map(([key, label]) => (
            <Field
              key={key}
              id={`oidc-${key}`}
              label={label}
              help={
                key === "admin_emails"
                  ? "These emails can sign in even outside the allowed domains. When no role grant exists, a verified email from the configured issuer can bootstrap the first owner. Once any grant exists, this list grants no role. Use Admin groups or Admin roles to grant administrator access."
                  : undefined
              }
            >
              <Input
                id={`oidc-${key}`}
                aria-describedby={
                  key === "admin_emails" ? `oidc-${key}-help` : undefined
                }
                value={fields[key]}
                onChange={(e) => update(key, e.target.value)}
              />
            </Field>
          ))}
          <p className="text-muted-foreground text-meta">
            Separate list values with commas. Leave endpoint overrides blank to
            use provider discovery.
          </p>
          <Field id="oidc-clock" label="Clock tolerance (seconds)">
            <Input
              id="oidc-clock"
              type="number"
              min={0}
              step={1}
              value={fields.clock_skew_seconds}
              onChange={(e) => update("clock_skew_seconds", e.target.value)}
            />
          </Field>
        </div>
      </details>
      <MutationError error={mutation.error} />
      <Save pending={mutation.isPending} label="Save sign-in" />
      <p className="text-muted-foreground text-meta">
        Changes apply to future sign-ins. Keep this session open until you test
        the provider.{" "}
        <a className="text-link underline" href="/setup">
          Owner recovery
        </a>
      </p>
    </form>
  );
}
function DefaultsSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.defaults,
    queryFn: async () => {
      const [defaults, profiles] = await Promise.all([
        adminFetch<DefaultsResponse>("/admin/api/settings/defaults"),
        api.profiles.list(),
      ]);
      return { ...defaults, profiles };
    },
  });
  const accessQuery = useQuery({
    queryKey: keys.accessDefaults,
    queryFn: api.userAccessDefaults.get,
  });
  return (
    <Section
      title="Defaults"
      summary={
        data && accessQuery.data
          ? {
              label: data.defaults.profile_id
                ? (data.profiles.find((p) => p.id === data.defaults.profile_id)
                    ?.name ?? "Default access plan")
                : "No default access plan",
              description: `${
                accessQuery.data.self_service_apps
                  ? "People can create apps"
                  : "Only owners create apps"
              } · ${
                accessQuery.data.auto_create_personal_app
                  ? "Personal app created at sign-in"
                  : "No automatic personal app"
              }`,
            }
          : undefined
      }
    >
      {isPending ? (
        <Loading />
      ) : isError ? (
        <SettingsError
          error={error}
          message="Defaults could not be loaded."
          onRetry={() => void refetch()}
        />
      ) : (
        <DefaultsForm data={data} />
      )}
      {accessQuery.isPending ? (
        <Loading />
      ) : accessQuery.isError ? (
        <SettingsError
          error={accessQuery.error}
          message="Personal app defaults could not be loaded."
          onRetry={() => void accessQuery.refetch()}
        />
      ) : (
        <AccessDefaultsForm initial={accessQuery.data} />
      )}
    </Section>
  );
}
function DefaultsForm({
  data,
}: {
  data: DefaultsResponse & {
    profiles: { id: string; name: string }[];
  };
}) {
  const owner = useViewer().role === "owner";
  const client = useQueryClient();
  const [plan, setPlan] = useDraft(data.defaults.profile_id ?? "none");
  const selectedPlan = data.profiles.find((profile) => profile.id === plan);
  const selectedPlanIsLegacy =
    selectedPlan !== undefined && isLegacyAccessPlan(selectedPlan.name);
  // Different authorities have separate saves: a failed app-options write cannot claim a plan was saved.
  const planSave = useMutation({
    mutationFn: () =>
      adminFetch("/admin/api/settings/defaults", {
        method: "PUT",
        body: { profile_id: plan === "none" ? null : plan },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.defaults });
      void client.invalidateQueries({ queryKey: keys.effective });
      toast.success("Default access plan saved");
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
          id="default-plan"
          label="Default access plan"
          help="Applies to everyone without a more specific identity rule, including existing people. No default plan adds no universal grant."
        >
          <Select value={plan} onValueChange={setPlan}>
            <SelectTrigger id="default-plan" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">No default plan</SelectItem>
              {!data.profiles.some((p) => p.id === "default-local") && (
                <SelectItem value="default-local">
                  Default local (built-in)
                </SelectItem>
              )}
              {!data.profiles.some((p) => p.id === "default") && (
                <SelectItem value="default">Default (built-in)</SelectItem>
              )}
              {plan !== "none" &&
                !["default", "default-local"].includes(plan) &&
                (selectedPlanIsLegacy || !selectedPlan) && (
                  <SelectItem value={plan}>Current plan</SelectItem>
                )}
              {data.profiles
                .filter((p) => !isLegacyAccessPlan(p.name))
                .map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.name}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </Field>
        <MutationError error={planSave.error} />
        <Save pending={planSave.isPending} label="Save default plan" />
        <Button variant="link" asChild>
          <Link href="/people">Manage access plans</Link>
        </Button>
      </form>

      <details>
        <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
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
function AccessDefaultsForm({ initial }: { initial: UserAccessDefaults }) {
  const client = useQueryClient();
  const [access, setAccess] = useDraft(initial);
  const accessSave = useMutation({
    mutationFn: () => api.userAccessDefaults.update(access),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.accessDefaults });
      void client.invalidateQueries({ queryKey: keys.effective });
      toast.success("Personal app defaults saved");
    },
  });
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        accessSave.mutate();
      }}
    >
      <Toggle
        id="self-service"
        label="Let people create their own apps"
        checked={access.self_service_apps}
        onChange={(value) => setAccess({ ...access, self_service_apps: value })}
      />
      <Toggle
        id="personal-app"
        label="Create a personal app at sign-in"
        checked={access.auto_create_personal_app}
        onChange={(value) =>
          setAccess({ ...access, auto_create_personal_app: value })
        }
      />
      <MutationError error={accessSave.error} />
      <Save pending={accessSave.isPending} label="Save app defaults" />
    </form>
  );
}
function MediaSection() {
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: keys.media,
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
        <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
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
        <summary className="text-meta flex min-h-8 cursor-pointer items-center font-medium max-sm:min-h-11">
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
        <MediaSection />
        <ExtensionsSection />
        <AdvancedSection />
      </div>
    </div>
  );
}
