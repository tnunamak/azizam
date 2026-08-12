"use client";

/** Admin → Settings — three independent grouped cards in order:
 *   1. Sign-in (OIDC config — status line + edit Dialog; secrets write-only)
 *   2. Extensions (package list + validate/enable flow)
 *   3. Media admission (existing override fields, product language, PUT on save)
 *
 * Spec: docs/design/WEB_UI_STEERING.md §"Settings"
 * Contract: docs/design/web-api-contract.md §"Surface 7 — Settings"
 *
 * Presentation law compliance:
 * - RelativeTime for every timestamp
 * - EntityRef is not applicable here (no entity IDs shown as primary content)
 * - No window.confirm — all destructive actions via ConfirmDialog
 * - No raw secrets rendered (client_secret treated write-only)
 * - Every group has loading skeleton + empty + error states
 * - Independent groups: no global save button */

import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { adminFetch, adminRegistryMutation, ApiError } from "@/lib/api";
import type { EffectiveOidcConfig, OidcConfigUpdate } from "@/lib/api";
import {
  Badge,
  Button,
  CopyField,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  EmptyOperationalState,
  Field,
  IncidentBanner,
  Input,
  PanelHeader,
  ProductPanel,
  RelativeTime,
  SectionActionHeader,
  Select,
  Skeleton,
  StatusDotLabel,
  Switch,
  toast,
} from "@/components/ui";

// ---------------------------------------------------------------------------
// Contract types for this surface

interface ExtensionPackage {
  id: string;
  version: string;
  source: ExtensionSource;
  enabled: boolean;
  enabled_at: number | null;
}

interface ExtensionSource {
  type: "local_directory" | "git";
  path?: string;
  url?: string;
  ref?: string;
}

interface ExtensionReview {
  ok: boolean;
  package: { id: string; version: string };
  errors: string[];
  warnings: string[];
  requires_code_trust: boolean;
  resolved_source: Record<string, unknown>;
  package_hash: string;
  object_counts: {
    services: number;
    protocol_frontends: number;
    capabilities: number;
    workflows: number;
  };
}

interface MediaJobAdmission {
  disabled_intents: string[];
  disabled_resource_classes: string[];
  reserved_tokens: string[];
  max_queue_depth: number | null;
  retry_after_seconds: number | null;
}

// ---------------------------------------------------------------------------
// Query keys (settings-local; scoped to avoid stomping the shared cache)

const settingsQueryKeys = {
  oidcConfig: ["settings", "oidc-config"] as const,
  extensions: ["settings", "extensions"] as const,
  overrides: ["settings", "overrides"] as const,
};

// ---------------------------------------------------------------------------
// API calls

async function fetchOidcConfig(): Promise<EffectiveOidcConfig | null> {
  const data = await adminFetch<{ oidc_config: EffectiveOidcConfig | null }>(
    "/admin/api/oidc-config",
  );
  return data.oidc_config;
}

async function saveOidcConfig(
  input: OidcConfigUpdate,
): Promise<EffectiveOidcConfig> {
  const data = await adminFetch<{ oidc_config: EffectiveOidcConfig }>(
    "/admin/api/oidc-config",
    { method: "PUT", body: input },
  );
  return data.oidc_config;
}

async function fetchExtensions(): Promise<Record<string, ExtensionPackage>> {
  const data = await adminFetch<{
    extension_packages: { packages: Record<string, ExtensionPackage> };
  }>("/admin/api/extensions");
  return data.extension_packages.packages;
}

async function validateExtension(
  source: ExtensionSource,
): Promise<ExtensionReview> {
  const data = await adminFetch<{ review: ExtensionReview }>(
    "/admin/api/extensions/validate",
    { method: "POST", body: { source } },
  );
  return data.review;
}

async function enableExtension(
  source: ExtensionSource,
  review: ExtensionReview,
): Promise<void> {
  await adminRegistryMutation("/admin/api/extensions/enable", {
    method: "POST",
    body: { source, review: { resolved_source: review.resolved_source } },
  });
}

async function fetchOverrides(): Promise<MediaJobAdmission> {
  const data = await adminFetch<{
    overrides: { media_job_admission: Partial<MediaJobAdmission> | null };
  }>("/admin/api/operator/overrides");
  const raw = data.overrides.media_job_admission ?? {};
  // Normalise: the API may return an empty object when nothing has been
  // configured yet (all defaults apply). Fill missing array/number fields so
  // joinList / Number conversions never see undefined.
  return {
    disabled_intents: raw.disabled_intents ?? [],
    disabled_resource_classes: raw.disabled_resource_classes ?? [],
    reserved_tokens: raw.reserved_tokens ?? [],
    max_queue_depth: raw.max_queue_depth ?? null,
    retry_after_seconds: raw.retry_after_seconds ?? null,
  };
}

async function saveMediaAdmission(
  input: Partial<MediaJobAdmission>,
): Promise<MediaJobAdmission> {
  const data = await adminFetch<{ media_job_admission: MediaJobAdmission }>(
    "/admin/api/operator/media-job-admission",
    { method: "PUT", body: input },
  );
  return data.media_job_admission;
}

// ---------------------------------------------------------------------------
// Shared helpers

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) {
      return "The service registry changed elsewhere (412 Precondition Failed). Refresh and retry.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

/** Comma-separated string ↔ string[] helpers for the array fields */
function joinList(arr: string[]): string {
  return arr.join(", ");
}
function splitList(str: string): string[] {
  return str
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// ---------------------------------------------------------------------------
// Shared error banner

function ErrorBanner({
  title,
  onRetry,
}: {
  title: string;
  onRetry: () => void;
}) {
  return (
    <IncidentBanner
      tone="bad"
      title={title}
      actions={
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    />
  );
}

// ---------------------------------------------------------------------------
function RestartBanner({ onDismiss }: { onDismiss: () => void }) {
  return (
    <IncidentBanner
      tone="info"
      title="Saved — sign-in settings apply live."
      detail="If sign-in access is lost, use an existing admin session or generate an admin key on the host and claim it at /setup/claim."
      onDismiss={onDismiss}
    />
  );
}

// ---------------------------------------------------------------------------
// Card wrapper — a quiet grouped card matching the shell rhythm

function SettingsCard({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <ProductPanel
      className={["flex flex-col gap-4", className ?? ""].join(" ")}
    >
      {children}
    </ProductPanel>
  );
}

function CardHeader({
  title,
  description,
  action,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <PanelHeader title={title} description={description} action={action} />
  );
}

// ---------------------------------------------------------------------------
// 1. SIGN-IN CARD

function SignInCard() {
  const queryClient = useQueryClient();
  const [editOpen, setEditOpen] = useState(false);
  const [showRestartBanner, setShowRestartBanner] = useState(false);

  const {
    data: oidcConfig,
    isPending,
    isError,
    refetch,
  } = useQuery({
    queryKey: settingsQueryKeys.oidcConfig,
    queryFn: fetchOidcConfig,
  });

  function handleSaved(updated: EffectiveOidcConfig) {
    queryClient.setQueryData(settingsQueryKeys.oidcConfig, updated);
    setEditOpen(false);
    setShowRestartBanner(true);
  }

  const isConfigured = oidcConfig !== null && oidcConfig !== undefined;

  return (
    <SettingsCard>
      <CardHeader
        title="Sign-in"
        description="Configure how users sign in with an external identity provider (OIDC)."
        action={
          <Button
            size="sm"
            onClick={() => setEditOpen(true)}
            disabled={isPending}
          >
            {isConfigured ? "Edit" : "Configure"}
          </Button>
        }
      />

      {isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-4 w-56" />
          <Skeleton className="h-4 w-40" />
        </div>
      ) : isError ? (
        <ErrorBanner
          title="Couldn't load sign-in settings."
          onRetry={() => void refetch()}
        />
      ) : !isConfigured ? (
        <p className="text-vf-small text-vf-muted">
          Not configured — users sign in with local accounts only.
        </p>
      ) : (
        <OidcStatusLine config={oidcConfig} />
      )}

      {showRestartBanner && (
        <RestartBanner onDismiss={() => setShowRestartBanner(false)} />
      )}

      <OidcEditDialog
        open={editOpen}
        onOpenChange={(open) => {
          if (!open) setEditOpen(false);
        }}
        existing={oidcConfig ?? null}
        onSaved={handleSaved}
      />
    </SettingsCard>
  );
}

function OidcStatusLine({ config }: { config: EffectiveOidcConfig }) {
  const provenance = Object.entries(config.provenance.fields)
    .filter(([, source]) => source !== "runtime_default")
    .map(([field, source]) => `${field}: ${source}`)
    .join(", ");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <StatusDotLabel
          tone={config.enabled ? "ok" : "warn"}
          label={config.enabled ? "Enabled" : "Disabled"}
          pulse={config.enabled}
        />
        {config.updated_at !== null && (
          <span className="text-vf-micro text-vf-muted">
            Updated <RelativeTime ts={config.updated_at} />
          </span>
        )}
      </div>
      {config.issuer && (
        <CopyField label="Issuer" value={config.issuer} monospace />
      )}
      {config.client_id && (
        <CopyField label="Client ID" value={config.client_id} monospace />
      )}
      <div className="text-vf-small text-vf-muted">
        Secret:{" "}
        {config.client_secret_set ? (
          <span className="text-vf-text">Saved (write-only — not shown)</span>
        ) : (
          <span className="text-vf-subtle">Not set</span>
        )}
      </div>
      <div className="text-vf-small text-vf-muted">
        Source:{" "}
        <span className="text-vf-text">{config.source} configuration</span> ·
        applies {config.apply_mode}
      </div>
      {config.admin_emails.length > 0 && (
        <div className="text-vf-small text-vf-muted">
          Admin emails:{" "}
          <span className="text-vf-text">{config.admin_emails.join(", ")}</span>
        </div>
      )}
      {provenance && (
        <div className="text-vf-micro text-vf-subtle">
          Field sources: {provenance}
        </div>
      )}
      <div className="text-vf-small text-vf-muted">
        Recovery: use an existing admin session, or generate an admin key on the
        host and claim it at <code>/setup/claim</code>.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// OIDC edit dialog

interface OidcFormState {
  enabled: boolean;
  issuer: string;
  client_id: string;
  /** Empty = preserve existing secret (patch semantics). */
  client_secret: string;
  redirect_uri: string;
  /** Comma-separated in the UI; split on save. */
  scopes: string;
  allowed_email_domains: string;
  admin_emails: string;
  admin_groups: string;
  admin_roles: string;
  authorization_endpoint: string;
  token_endpoint: string;
  jwks_uri: string;
  clock_skew_seconds: string;
  groups_claim: string;
}

function oidcFormDefaults(existing: EffectiveOidcConfig | null): OidcFormState {
  if (!existing) {
    return {
      enabled: true,
      issuer: "",
      client_id: "",
      client_secret: "",
      redirect_uri: "",
      scopes: "openid, email, profile",
      allowed_email_domains: "",
      admin_emails: "",
      admin_groups: "",
      admin_roles: "",
      authorization_endpoint: "",
      token_endpoint: "",
      jwks_uri: "",
      clock_skew_seconds: "60",
      groups_claim: "groups",
    };
  }
  return {
    enabled: existing.enabled,
    issuer: existing.issuer,
    client_id: existing.client_id,
    // Leave blank so the user must explicitly re-enter to change it.
    client_secret: "",
    redirect_uri: existing.redirect_uri,
    scopes: joinList(existing.scopes),
    allowed_email_domains: joinList(existing.allowed_email_domains),
    admin_emails: joinList(existing.admin_emails),
    admin_groups: joinList(existing.admin_groups),
    admin_roles: joinList(existing.admin_roles),
    authorization_endpoint: existing.authorization_endpoint,
    token_endpoint: existing.token_endpoint,
    jwks_uri: existing.jwks_uri,
    clock_skew_seconds: String(existing.clock_skew_seconds ?? 60),
    groups_claim: existing.groups_claim ?? "groups",
  };
}

function OidcEditDialog({
  open,
  onOpenChange,
  existing,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existing: EffectiveOidcConfig | null;
  onSaved: (updated: EffectiveOidcConfig) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <OidcEditDialogContent
          existing={existing}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

function OidcEditDialogContent({
  existing,
  onOpenChange,
  onSaved,
}: {
  existing: EffectiveOidcConfig | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (updated: EffectiveOidcConfig) => void;
}) {
  const [form, setForm] = useState<OidcFormState>(() =>
    oidcFormDefaults(existing),
  );
  const [formError, setFormError] = useState<string | null>(null);

  function set<K extends keyof OidcFormState>(key: K, value: OidcFormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const save = useMutation({
    mutationFn: () => {
      const payload: OidcConfigUpdate = {
        enabled: form.enabled,
        issuer: form.issuer.trim(),
        client_id: form.client_id.trim(),
        redirect_uri: form.redirect_uri.trim(),
        scopes: splitList(form.scopes),
        allowed_email_domains: splitList(form.allowed_email_domains),
        admin_emails: splitList(form.admin_emails),
        admin_groups: splitList(form.admin_groups),
        admin_roles: splitList(form.admin_roles),
        authorization_endpoint: form.authorization_endpoint.trim(),
        token_endpoint: form.token_endpoint.trim(),
        jwks_uri: form.jwks_uri.trim(),
        clock_skew_seconds: Number(form.clock_skew_seconds) || 60,
        groups_claim: form.groups_claim.trim() || "groups",
      };
      // Only include client_secret when the user typed something new.
      // Blank / "<redacted>" → preserved by the server (patch semantics).
      const secret = form.client_secret.trim();
      if (secret && secret !== "<redacted>") {
        payload.client_secret = secret;
      }
      return saveOidcConfig(payload);
    },
    onSuccess: (updated) => {
      toast.success("Sign-in settings saved");
      onSaved(updated);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    save.mutate();
  }

  return (
    <DialogContent className="max-w-xl">
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>
            {existing ? "Edit sign-in settings" : "Configure sign-in"}
          </DialogTitle>
          <DialogDescription>
            Most settings apply immediately. The signing key and cookie name are
            captured at startup — changing them requires a gateway restart to
            take effect. The client secret is write-only — leave blank to keep
            the current value.
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          {/* Enabled toggle */}
          <div className="flex items-center justify-between gap-3">
            <div className="flex flex-col gap-0.5">
              <span className="text-vf-small text-vf-text font-medium">
                Enabled
              </span>
              <span className="text-vf-small text-vf-subtle">
                Allow sign-in via this OIDC provider
              </span>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(checked) => set("enabled", checked)}
              aria-label="Enable OIDC sign-in"
              disabled={save.isPending}
            />
          </div>

          {/* Core fields */}
          <Field label="Issuer URL" required>
            <Input
              value={form.issuer}
              onChange={(e) => set("issuer", e.target.value)}
              placeholder="https://accounts.google.com"
              disabled={save.isPending}
            />
          </Field>

          <Field label="Client ID" required>
            <Input
              value={form.client_id}
              onChange={(e) => set("client_id", e.target.value)}
              placeholder="my-app.apps.googleusercontent.com"
              disabled={save.isPending}
            />
          </Field>

          <Field
            label="Client secret"
            help={
              existing?.client_secret_set
                ? "A secret is already saved. Leave blank to keep it."
                : "Paste your client secret. It will not be shown again."
            }
          >
            <Input
              type="password"
              value={form.client_secret}
              onChange={(e) => set("client_secret", e.target.value)}
              placeholder={
                existing?.client_secret_set
                  ? "•••• (leave blank to keep)"
                  : "Paste secret"
              }
              autoComplete="new-password"
              disabled={save.isPending}
            />
          </Field>

          <Field
            label="Redirect URI"
            help="Must match what you configured in your identity provider."
          >
            <Input
              value={form.redirect_uri}
              onChange={(e) => set("redirect_uri", e.target.value)}
              placeholder="https://gateway.example.com/auth/callback"
              disabled={save.isPending}
            />
          </Field>

          {/* Access control */}
          <Field
            label="Allowed email domains"
            help="Comma-separated. Empty = any authenticated user."
          >
            <Input
              value={form.allowed_email_domains}
              onChange={(e) => set("allowed_email_domains", e.target.value)}
              placeholder="example.com, corp.example.com"
              disabled={save.isPending}
            />
          </Field>

          <Field
            label="Admin emails"
            help="Comma-separated email addresses that get admin access."
          >
            <Input
              value={form.admin_emails}
              onChange={(e) => set("admin_emails", e.target.value)}
              placeholder="ops@example.com"
              disabled={save.isPending}
            />
          </Field>

          {/* Advanced — always visible but low-prominence */}
          <details className="flex flex-col gap-4">
            <summary className="text-vf-small text-vf-muted cursor-pointer py-1 select-none">
              Advanced options
            </summary>
            <div className="mt-3 flex flex-col gap-4">
              <Field
                label="Scopes"
                help="Comma-separated. Defaults: openid, email, profile."
              >
                <Input
                  value={form.scopes}
                  onChange={(e) => set("scopes", e.target.value)}
                  placeholder="openid, email, profile"
                  disabled={save.isPending}
                />
              </Field>

              <Field
                label="Admin groups"
                help="OIDC group claims that grant admin access."
              >
                <Input
                  value={form.admin_groups}
                  onChange={(e) => set("admin_groups", e.target.value)}
                  placeholder="gateway-admins"
                  disabled={save.isPending}
                />
              </Field>

              <Field
                label="Admin roles"
                help="OIDC role claims that grant admin access."
              >
                <Input
                  value={form.admin_roles}
                  onChange={(e) => set("admin_roles", e.target.value)}
                  placeholder="admin"
                  disabled={save.isPending}
                />
              </Field>

              <Field
                label="Groups claim"
                help={
                  "The ID-token claim that carries the user's group memberships. " +
                  "Used by admin groups (above) to grant admin access, and by " +
                  "Users → profile mappings to assign access by group. " +
                  "Common values: “groups” (default), “cognito:groups” (AWS Cognito), “roles”."
                }
              >
                <Input
                  value={form.groups_claim}
                  onChange={(e) => set("groups_claim", e.target.value)}
                  placeholder="groups"
                  disabled={save.isPending}
                />
              </Field>

              <Field
                label="Authorization endpoint"
                help="Leave blank to use the issuer discovery document."
              >
                <Input
                  value={form.authorization_endpoint}
                  onChange={(e) =>
                    set("authorization_endpoint", e.target.value)
                  }
                  placeholder="https://accounts.google.com/o/oauth2/auth"
                  disabled={save.isPending}
                />
              </Field>

              <Field label="Token endpoint">
                <Input
                  value={form.token_endpoint}
                  onChange={(e) => set("token_endpoint", e.target.value)}
                  placeholder="https://oauth2.googleapis.com/token"
                  disabled={save.isPending}
                />
              </Field>

              <Field label="JWKS URI">
                <Input
                  value={form.jwks_uri}
                  onChange={(e) => set("jwks_uri", e.target.value)}
                  placeholder="https://www.googleapis.com/oauth2/v3/certs"
                  disabled={save.isPending}
                />
              </Field>

              <Field
                label="Clock skew (seconds)"
                help="Tolerance for token timestamp drift."
              >
                <Input
                  type="number"
                  min="0"
                  value={form.clock_skew_seconds}
                  onChange={(e) => set("clock_skew_seconds", e.target.value)}
                  placeholder="60"
                  disabled={save.isPending}
                />
              </Field>
            </div>
          </details>

          {formError && (
            <p role="alert" className="text-vf-small text-vf-bad">
              {formError}
            </p>
          )}
        </DialogBody>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// 2. EXTENSIONS CARD

function ExtensionsCard() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);

  const {
    data: packages,
    isPending,
    isError,
    refetch,
  } = useQuery({
    queryKey: settingsQueryKeys.extensions,
    queryFn: fetchExtensions,
  });

  const packageList = packages ? Object.values(packages) : [];

  function handleEnabled() {
    void queryClient.invalidateQueries({
      queryKey: settingsQueryKeys.extensions,
    });
    setAddOpen(false);
    toast.success("Extension enabled");
  }

  return (
    <SettingsCard>
      <CardHeader
        title="Extensions"
        description="Load additional services and capabilities from extension packages."
        action={
          <Button
            size="sm"
            onClick={() => setAddOpen(true)}
            disabled={isPending}
          >
            Add extension
          </Button>
        }
      />

      {isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : isError ? (
        <ErrorBanner
          title="Couldn't load extensions."
          onRetry={() => void refetch()}
        />
      ) : packageList.length === 0 ? (
        <EmptyOperationalState
          title="No extensions installed."
          action={
            <Button size="sm" onClick={() => setAddOpen(true)}>
              Add extension
            </Button>
          }
        />
      ) : (
        <ul className="divide-vf-border/60 flex flex-col divide-y">
          {packageList.map((pkg) => (
            <ExtensionRow key={pkg.id} pkg={pkg} />
          ))}
        </ul>
      )}

      <AddExtensionDialog
        open={addOpen}
        onOpenChange={(open) => {
          if (!open) setAddOpen(false);
        }}
        onEnabled={handleEnabled}
      />
    </SettingsCard>
  );
}

function ExtensionRow({ pkg }: { pkg: ExtensionPackage }) {
  const sourceLabel =
    pkg.source.type === "local_directory"
      ? (pkg.source.path ?? "local directory")
      : (pkg.source.url ?? "git");

  return (
    <li className="flex items-start justify-between gap-3 py-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-vf-body text-vf-text font-medium">{pkg.id}</span>
        <span className="text-vf-small text-vf-subtle font-mono">
          v{pkg.version} · {sourceLabel}
        </span>
        {pkg.enabled_at !== null && (
          <span className="text-vf-micro text-vf-subtle">
            Enabled <RelativeTime ts={pkg.enabled_at} />
          </span>
        )}
      </div>
      <Badge sm tone={pkg.enabled ? "ok" : "neutral"}>
        {pkg.enabled ? "enabled" : "disabled"}
      </Badge>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Add extension dialog — two-step: validate → enable

type AddExtensionStep = "form" | "review";

function AddExtensionDialog({
  open,
  onOpenChange,
  onEnabled,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onEnabled: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <AddExtensionDialogContent
          onOpenChange={onOpenChange}
          onEnabled={onEnabled}
        />
      )}
    </Dialog>
  );
}

function AddExtensionDialogContent({
  onOpenChange,
  onEnabled,
}: {
  onOpenChange: (open: boolean) => void;
  onEnabled: () => void;
}) {
  const [step, setStep] = useState<AddExtensionStep>("form");
  const [sourceType, setSourceType] = useState<"local_directory" | "git">(
    "local_directory",
  );
  const [sourcePath, setSourcePath] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [gitRef, setGitRef] = useState("");
  const [review, setReview] = useState<ExtensionReview | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const validate = useMutation({
    mutationFn: () => {
      const source: ExtensionSource =
        sourceType === "local_directory"
          ? { type: "local_directory", path: sourcePath.trim() }
          : {
              type: "git",
              url: gitUrl.trim(),
              ref: gitRef.trim() || undefined,
            };
      return validateExtension(source);
    },
    onSuccess: (result) => {
      setReview(result);
      setStep("review");
      setFormError(null);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const enable = useMutation({
    mutationFn: () => {
      if (!review) throw new Error("No review result");
      const source: ExtensionSource =
        sourceType === "local_directory"
          ? { type: "local_directory", path: sourcePath.trim() }
          : {
              type: "git",
              url: gitUrl.trim(),
              ref: gitRef.trim() || undefined,
            };
      return enableExtension(source, review);
    },
    onSuccess: () => onEnabled(),
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleValidate(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    validate.mutate();
  }

  function handleEnable(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    enable.mutate();
  }

  if (step === "review" && review) {
    return (
      <DialogContent className="max-w-lg">
        <DialogForm onSubmit={handleEnable}>
          <DialogHeader>
            <DialogTitle>Review extension</DialogTitle>
            <DialogDescription>
              Validate passed. Review the details before enabling.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
              <dt className="text-vf-small text-vf-muted">Package</dt>
              <dd className="text-vf-small text-vf-text font-medium">
                {review.package.id}
              </dd>

              <dt className="text-vf-small text-vf-muted">Version</dt>
              <dd className="text-vf-small text-vf-text font-mono">
                {review.package.version}
              </dd>

              <dt className="text-vf-small text-vf-muted">Objects</dt>
              <dd className="text-vf-small text-vf-text">
                {[
                  review.object_counts.services > 0 &&
                    `${review.object_counts.services} service${review.object_counts.services > 1 ? "s" : ""}`,
                  review.object_counts.protocol_frontends > 0 &&
                    `${review.object_counts.protocol_frontends} frontend${review.object_counts.protocol_frontends > 1 ? "s" : ""}`,
                  review.object_counts.capabilities > 0 &&
                    `${review.object_counts.capabilities} capability`,
                  review.object_counts.workflows > 0 &&
                    `${review.object_counts.workflows} workflow${review.object_counts.workflows > 1 ? "s" : ""}`,
                ]
                  .filter(Boolean)
                  .join(", ") || "No objects"}
              </dd>
            </dl>

            {review.requires_code_trust && (
              <div
                role="alert"
                className="rounded-vf-md border-vf-warn/40 bg-vf-warn/10 border p-3"
              >
                <p className="text-vf-small text-vf-text">
                  <strong>Requires code trust</strong> — this extension runs
                  code on the gateway. Only enable packages you control or fully
                  trust.
                </p>
              </div>
            )}

            {review.warnings.length > 0 && (
              <div className="flex flex-col gap-1">
                {review.warnings.map((w, i) => (
                  <p key={i} className="text-vf-small text-vf-warn">
                    {w}
                  </p>
                ))}
              </div>
            )}

            {review.errors.length > 0 && (
              <div className="flex flex-col gap-1">
                {review.errors.map((e, i) => (
                  <p key={i} className="text-vf-small text-vf-bad">
                    {e}
                  </p>
                ))}
              </div>
            )}

            {formError && (
              <p role="alert" className="text-vf-small text-vf-bad">
                {formError}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => {
                setStep("form");
                setFormError(null);
              }}
              disabled={enable.isPending}
            >
              Back
            </Button>
            <Button
              type="submit"
              variant="primary"
              loading={enable.isPending}
              disabled={!review.ok || review.errors.length > 0}
            >
              Enable extension
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    );
  }

  return (
    <DialogContent className="max-w-lg">
      <DialogForm onSubmit={handleValidate}>
        <DialogHeader>
          <DialogTitle>Add extension</DialogTitle>
          <DialogDescription>
            Extensions add services and capabilities to the gateway. The source
            is validated before enabling.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field label="Source type">
            <Select
              value={sourceType}
              onChange={(e) =>
                setSourceType(e.target.value as "local_directory" | "git")
              }
              disabled={validate.isPending}
            >
              <option value="local_directory">Local directory</option>
              <option value="git">Git repository</option>
            </Select>
          </Field>

          {sourceType === "local_directory" ? (
            <Field
              label="Directory path"
              required
              help="Absolute path on the gateway server."
            >
              <Input
                value={sourcePath}
                onChange={(e) => setSourcePath(e.target.value)}
                placeholder="/extensions/my-package"
                autoFocus
                disabled={validate.isPending}
              />
            </Field>
          ) : (
            <>
              <Field label="Git URL" required>
                <Input
                  value={gitUrl}
                  onChange={(e) => setGitUrl(e.target.value)}
                  placeholder="https://github.com/org/extension-pkg"
                  autoFocus
                  disabled={validate.isPending}
                />
              </Field>
              <Field
                label="Ref"
                help="Branch, tag, or commit SHA. Leave blank for the default branch."
              >
                <Input
                  value={gitRef}
                  onChange={(e) => setGitRef(e.target.value)}
                  placeholder="main"
                  disabled={validate.isPending}
                />
              </Field>
            </>
          )}

          {formError && (
            <p role="alert" className="text-vf-small text-vf-bad">
              {formError}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={validate.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={validate.isPending}>
            Validate
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// 3. MEDIA ADMISSION CARD

function MediaAdmissionCard() {
  const queryClient = useQueryClient();
  const [editOpen, setEditOpen] = useState(false);

  const {
    data: admission,
    isPending,
    isError,
    refetch,
  } = useQuery({
    queryKey: settingsQueryKeys.overrides,
    queryFn: fetchOverrides,
  });

  function handleSaved(updated: MediaJobAdmission) {
    queryClient.setQueryData(settingsQueryKeys.overrides, updated);
    setEditOpen(false);
  }

  return (
    <SettingsCard>
      <CardHeader
        title="Media admission"
        description="Control which media generation jobs the gateway admits. Changes take effect immediately."
        action={
          <Button
            size="sm"
            onClick={() => setEditOpen(true)}
            disabled={isPending}
          >
            Edit
          </Button>
        }
      />

      {isPending ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-3/4" />
        </div>
      ) : isError ? (
        <ErrorBanner
          title="Couldn't load media admission settings."
          onRetry={() => void refetch()}
        />
      ) : (
        <MediaAdmissionSummary admission={admission} />
      )}

      <MediaAdmissionEditDialog
        open={editOpen}
        onOpenChange={(open) => {
          if (!open) setEditOpen(false);
        }}
        admission={admission ?? null}
        onSaved={handleSaved}
      />
    </SettingsCard>
  );
}

function MediaAdmissionSummary({
  admission,
}: {
  admission: MediaJobAdmission | undefined;
}) {
  if (!admission) {
    return (
      <p className="text-vf-small text-vf-muted">
        No media admission settings are configured.
      </p>
    );
  }

  const rows: Array<[string, string]> = [];
  if (admission.disabled_intents.length > 0) {
    rows.push(["Blocked intents", joinList(admission.disabled_intents)]);
  }
  if (admission.disabled_resource_classes.length > 0) {
    rows.push([
      "Blocked resources",
      joinList(admission.disabled_resource_classes),
    ]);
  }
  if (admission.reserved_tokens.length > 0) {
    rows.push(["Reserved tokens", joinList(admission.reserved_tokens)]);
  }
  if (admission.max_queue_depth !== null) {
    rows.push(["Max queue depth", String(admission.max_queue_depth)]);
  }
  if (admission.retry_after_seconds !== null) {
    rows.push(["Retry after", `${admission.retry_after_seconds}s`]);
  }

  if (rows.length === 0) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Badge sm tone="ok">
          clear
        </Badge>
        <span className="text-vf-small text-vf-muted">
          No blocked media intents, resource classes, queue caps, or retry hints
          are configured.
        </span>
      </div>
    );
  }

  return (
    <dl className="divide-vf-border/60 flex flex-col divide-y">
      {rows.map(([label, value]) => (
        <div
          key={label}
          className="grid gap-1 py-2 sm:grid-cols-[11rem_minmax(0,1fr)]"
        >
          <dt className="text-vf-small text-vf-muted">{label}</dt>
          <dd className="text-vf-small text-vf-text min-w-0 font-mono break-words">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function MediaAdmissionEditDialog({
  open,
  onOpenChange,
  admission,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  admission: MediaJobAdmission | null;
  onSaved: (updated: MediaJobAdmission) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <MediaAdmissionEditDialogContent
          admission={admission}
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

function MediaAdmissionEditDialogContent({
  admission,
  onOpenChange,
  onSaved,
}: {
  admission: MediaJobAdmission | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (updated: MediaJobAdmission) => void;
}) {
  const [disabledIntents, setDisabledIntents] = useState(
    joinList(admission?.disabled_intents ?? []),
  );
  const [disabledResourceClasses, setDisabledResourceClasses] = useState(
    joinList(admission?.disabled_resource_classes ?? []),
  );
  const [reservedTokens, setReservedTokens] = useState(
    joinList(admission?.reserved_tokens ?? []),
  );
  const [maxQueueDepth, setMaxQueueDepth] = useState(
    admission?.max_queue_depth !== null &&
      admission?.max_queue_depth !== undefined
      ? String(admission.max_queue_depth)
      : "",
  );
  const [retryAfterSeconds, setRetryAfterSeconds] = useState(
    admission?.retry_after_seconds !== null &&
      admission?.retry_after_seconds !== undefined
      ? String(admission.retry_after_seconds)
      : "",
  );
  const [formError, setFormError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      const payload: Partial<MediaJobAdmission> = {
        disabled_intents: splitList(disabledIntents),
        disabled_resource_classes: splitList(disabledResourceClasses),
        reserved_tokens: splitList(reservedTokens),
        max_queue_depth:
          maxQueueDepth.trim() !== "" ? Number(maxQueueDepth) : null,
        retry_after_seconds:
          retryAfterSeconds.trim() !== "" ? Number(retryAfterSeconds) : null,
      };
      return saveMediaAdmission(payload);
    },
    onSuccess: (updated) => {
      toast.success("Media admission settings saved");
      setFormError(null);
      onSaved(updated);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    save.mutate();
  }

  return (
    <DialogContent className="max-w-lg">
      <DialogForm onSubmit={handleSubmit}>
        <DialogHeader>
          <DialogTitle>Edit media admission</DialogTitle>
          <DialogDescription>
            Reject expensive or unavailable media jobs before they enter the
            queue.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <Field
            label="Blocked intents"
            help="Comma-separated. Jobs matching these intents are rejected at the gate."
          >
            <Input
              value={disabledIntents}
              onChange={(e) => setDisabledIntents(e.target.value)}
              placeholder="video.generate, image.generate"
              disabled={save.isPending}
            />
          </Field>

          <Field
            label="Blocked resource classes"
            help="Comma-separated resource class tags. Jobs that require these classes are rejected."
          >
            <Input
              value={disabledResourceClasses}
              onChange={(e) => setDisabledResourceClasses(e.target.value)}
              placeholder="gpu"
              disabled={save.isPending}
            />
          </Field>

          <Field
            label="Reserved token tags"
            help="Comma-separated. Jobs must hold one of these tokens to be admitted."
          >
            <Input
              value={reservedTokens}
              onChange={(e) => setReservedTokens(e.target.value)}
              placeholder="gpu"
              disabled={save.isPending}
            />
          </Field>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="Max queue depth" help="Leave blank for unlimited.">
              <Input
                type="number"
                min="0"
                value={maxQueueDepth}
                onChange={(e) => setMaxQueueDepth(e.target.value)}
                placeholder="Unlimited"
                disabled={save.isPending}
              />
            </Field>

            <Field
              label="Retry after (seconds)"
              help="Suggested retry delay sent with rejections."
            >
              <Input
                type="number"
                min="0"
                value={retryAfterSeconds}
                onChange={(e) => setRetryAfterSeconds(e.target.value)}
                placeholder="No suggestion"
                disabled={save.isPending}
              />
            </Field>
          </div>

          {formError && (
            <p role="alert" className="text-vf-small text-vf-bad">
              {formError}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save
          </Button>
        </DialogFooter>
      </DialogForm>
    </DialogContent>
  );
}

// ---------------------------------------------------------------------------
// Page

export default function AdminSettingsPage() {
  return (
    <div className="flex flex-col gap-10">
      <SectionActionHeader
        kicker="gateway"
        title="Settings"
        description="Sign-in, extensions, and gateway-wide configuration."
      />

      <div className="flex flex-col gap-6">
        <SignInCard />
        <ExtensionsCard />
        <MediaAdmissionCard />
      </div>
    </div>
  );
}
