"use client";
import { useId, type ReactNode } from "react";
import { CopyField } from "@/components/product/copy-field";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import type { Discovery, ProviderConnection } from "@/lib/signin-api";
import {
  Block,
  DisclosureButton,
  FoundLine,
  LinkButton,
  useReadOnly,
} from "./bits";
import {
  connectionIntro,
  DEFAULTS,
  hostOr,
  nameHelp,
  type ProviderLabel,
} from "./copy";
import type { DiscoveryState } from "./use-discovery";

type Patch = (change: Partial<ProviderConnection>) => void;

const ENDPOINTS = [
  ["authorization_endpoint", "Sign-in page address"],
  ["token_endpoint", "Token address"],
  ["jwks_uri", "Signing keys address"],
] as const;

export function ConnectionBlock({
  connection,
  label,
  redirectUri,
  secretSaved,
  discovery,
  found,
  open,
  onOpenChange,
  advancedOpen,
  onAdvancedOpenChange,
  testedWhen,
  groupsDetected,
  onChange,
  children,
}: {
  connection: ProviderConnection;
  label: ProviderLabel;
  redirectUri: string;
  secretSaved: boolean;
  discovery: DiscoveryState;
  found: Discovery | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  advancedOpen: boolean;
  onAdvancedOpenChange: (open: boolean) => void;
  /** "today at 09:14" when this connection passed a test. */
  testedWhen: string | null;
  groupsDetected: boolean;
  onChange: Patch;
  /** The Test sign-in row and its result. */
  children: ReactNode;
}) {
  const readOnly = useReadOnly();
  const panel = useId();
  const advanced = useId();
  const setUp = Boolean(connection.address.trim() && connection.client_id.trim());
  return (
    <Block
      title="Connect your provider"
      description={connectionIntro(label)}
      id="sign-in-connect"
    >
      <div className="flex items-center gap-2 px-4">
        <DisclosureButton
          open={open}
          onClick={() => onOpenChange(!open)}
          controls={panel}
          className="min-h-14 min-w-0 flex-1"
        >
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
            {setUp ? (
              <>
                <span className="font-medium">{label.name || "Your provider"}</span>{" "}
                <span className="text-muted-foreground text-meta">
                  {label.typed ? ` · ${hostOr(label)}` : ""} · client{" "}
                  {connection.client_id.trim()}
                  {testedWhen ? ` · tested ${testedWhen}` : ""}
                </span>
              </>
            ) : (
              <span className="font-medium">Not set up</span>
            )}
          </span>
          <span
            aria-hidden="true"
            className="rounded-control border-border bg-card text-meta inline-flex h-8 items-center border px-3 font-medium max-sm:h-11"
          >
            {open ? "Hide" : "Edit"}
          </span>
        </DisclosureButton>
      </div>
      {open && (
        <div id={panel} className="border-t">
          <div className="flex flex-col gap-5 px-4 py-5">
            <div>
              <CopyField
                label="Copy this into your provider"
                buttonLabel="Copy redirect address"
                value={redirectUri}
              />
              <p className="text-muted-foreground text-meta mt-1">
                Your provider may call it Redirect URI or callback URL.
              </p>
            </div>
            <Field
              label="Provider address"
              help="Paste the address once. Azizam finds the rest."
            >
              <Input
                type="url"
                autoComplete="off"
                spellCheck={false}
                placeholder="https://id.example.com/realms/home"
                value={connection.address}
                disabled={readOnly}
                onChange={(e) => onChange({ address: e.target.value })}
              />
              {discovery === "wait" && (
                <FoundLine tone="wait">Checking the address...</FoundLine>
              )}
              {discovery === "bad" && (
                <FoundLine tone="bad">
                  Could not reach a sign-in provider at that address. Check it
                  and try again.
                </FoundLine>
              )}
              {discovery === "ok" && (
                <FoundLine tone="ok">
                  Found a sign-in provider at {found?.host || label.host}.
                </FoundLine>
              )}
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Client ID">
                <Input
                  autoComplete="off"
                  spellCheck={false}
                  value={connection.client_id}
                  disabled={readOnly}
                  onChange={(e) => onChange({ client_id: e.target.value })}
                />
              </Field>
              <Field
                label="Client secret"
                help={
                  secretSaved && !connection.client_secret
                    ? "Leave blank to keep the saved secret."
                    : "Paste the secret from your provider."
                }
              >
                <Input
                  type="password"
                  autoComplete="new-password"
                  placeholder={secretSaved ? "Saved" : ""}
                  value={connection.client_secret}
                  disabled={readOnly}
                  onChange={(e) => onChange({ client_secret: e.target.value })}
                />
              </Field>
            </div>
            <Field label="Name" help={nameHelp(label)}>
              <Input
                autoComplete="off"
                placeholder={label.host || "For example Family account or Home login"}
                value={connection.name}
                disabled={readOnly}
                onChange={(e) => onChange({ name: e.target.value })}
              />
            </Field>
          </div>
          <div className="border-t px-4">
            <DisclosureButton
              open={advancedOpen}
              onClick={() => onAdvancedOpenChange(!advancedOpen)}
              controls={advanced}
              className="text-link text-meta min-h-12 font-medium"
            >
              Advanced
            </DisclosureButton>
          </div>
          {advancedOpen && (
            <AdvancedFields
              id={advanced}
              connection={connection}
              found={found}
              discovered={discovery === "ok"}
              groupsDetected={groupsDetected}
              onChange={onChange}
            />
          )}
        </div>
      )}
      <div className="border-t">{children}</div>
    </Block>
  );
}

function AdvancedFields({
  id,
  connection,
  found,
  discovered,
  groupsDetected,
  onChange,
}: {
  id: string;
  connection: ProviderConnection;
  found: Discovery | null;
  discovered: boolean;
  groupsDetected: boolean;
  onChange: Patch;
}) {
  const readOnly = useReadOnly();
  const hasAddress = Boolean(connection.address.trim());
  return (
    <div id={id} className="flex flex-col gap-4 px-4 pt-1 pb-5">
      <p className="text-muted-foreground text-meta">
        Filled in from your provider. Change one only if your provider tells
        you to.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <AdvancedField
          label="Scopes"
          mono
          value={connection.scopes}
          tag={tagFor({
            value: connection.scopes,
            fallback: DEFAULTS.scopes,
            hasAddress,
            normal: "from discovery",
          })}
          disabled={readOnly}
          onChange={(scopes) => onChange({ scopes })}
          onReset={() => onChange({ scopes: DEFAULTS.scopes })}
        />
        <AdvancedField
          label="Name of the groups list"
          mono
          value={connection.groups_claim}
          tag={tagFor({
            value: connection.groups_claim,
            fallback: DEFAULTS.groupsClaim,
            hasAddress,
            normal: groupsDetected ? "detected at last test" : "from discovery",
          })}
          disabled={readOnly}
          onChange={(groups_claim) => onChange({ groups_claim })}
          onReset={() => onChange({ groups_claim: DEFAULTS.groupsClaim })}
        />
      </div>
      {ENDPOINTS.map(([key, text]) => {
        const found_ = discovered ? (found?.[key] ?? "") : "";
        const shown = connection[key] || found_;
        return (
          <AdvancedField
            key={key}
            label={text}
            mono
            value={shown}
            placeholder={hasAddress ? "Found from the provider address" : ""}
            tag={tagFor({
              value: connection[key],
              fallback: found_,
              hasAddress,
              normal: "from discovery",
            })}
            disabled={readOnly}
            onChange={(typed) =>
              onChange({ [key]: typed === found_ ? "" : typed })
            }
            onReset={() => onChange({ [key]: "" })}
          />
        );
      })}
      <div className="max-w-65">
        <AdvancedField
          label="Clock tolerance (seconds)"
          type="number"
          value={String(connection.clock_skew_seconds)}
          tag={
            connection.clock_skew_seconds !== DEFAULTS.clockSkewSeconds
              ? { kind: "over" }
              : { kind: "normal", text: "default" }
          }
          disabled={readOnly}
          onChange={(v) =>
            onChange({ clock_skew_seconds: v === "" ? 0 : Number(v) })
          }
          onReset={() =>
            onChange({ clock_skew_seconds: DEFAULTS.clockSkewSeconds })
          }
        />
      </div>
    </div>
  );
}

type Tag = { kind: "normal"; text: string } | { kind: "over" };

function tagFor(a: {
  value: string;
  fallback: string;
  hasAddress: boolean;
  normal: string;
}): Tag {
  if (a.value.trim() && a.value !== a.fallback) return { kind: "over" };
  if (!a.value.trim() && !a.fallback && !a.hasAddress)
    return { kind: "normal", text: "after you enter the address" };
  return { kind: "normal", text: a.normal };
}

function AdvancedField({
  label,
  value,
  tag,
  mono,
  type,
  placeholder,
  disabled,
  onChange,
  onReset,
}: {
  label: string;
  value: string;
  tag: Tag;
  mono?: boolean;
  type?: string;
  placeholder?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const id = useId();
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="text-label">
          {label}
        </label>
        {tag.kind === "over" ? (
          <span className="bg-warning-tint text-warning rounded-badge text-badge inline-flex items-center gap-1.5 px-1.5">
            overridden
            {!disabled && (
              <LinkButton onClick={onReset} className="text-badge max-sm:min-h-0">
                Use the provider’s value
              </LinkButton>
            )}
          </span>
        ) : (
          <span className="bg-muted text-subtle-foreground rounded-badge text-badge inline-flex px-1.5">
            {tag.text}
          </span>
        )}
      </div>
      <Input
        id={id}
        size="sm"
        type={type}
        min={type === "number" ? 0 : undefined}
        placeholder={placeholder}
        className={mono ? "font-mono" : undefined}
        value={value}
        disabled={disabled}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
