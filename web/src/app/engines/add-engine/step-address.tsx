"use client";

import { useId } from "react";
import { Check, CircleCheck, CircleX, CircleAlert } from "lucide-react";
import type { EngineDefinition, Finding, FindingAction } from "@/lib/engines-api";
import { Input } from "@/components/ui/input";
import { CallLine } from "../parts/call-line";
import { FindingCard } from "../parts/finding-card";
import { FormField, LinkButton, Note, OwnerOnlyTag } from "../parts/form";
import { fieldOf } from "./draft";

/** Under the variable-name box, after a refused key: where to look, besides the server's own reason. */
const ENV_KEY_CONTEXT =
  "Azizam could not use the key from this variable. Check the variable name and its value on the computer running Azizam, then check again.";

export interface ConnectionDraft {
  address: string;
  key: string;
  envOpen: boolean;
  envName: string;
}

export interface FieldErrors {
  address?: string;
  api_key?: string;
  api_key_env?: string;
}

/** The fields of step 2, drawn from the definition: address, key, or the key's variable name. */
export function ConnectionFields({
  definition,
  draft,
  onChange,
  owner,
  finding,
  errors,
  disabled,
  onChangeKind,
  onFixPath,
  onOpenExisting,
  onRecheck,
}: {
  definition: EngineDefinition;
  draft: ConnectionDraft;
  onChange: (patch: Partial<ConnectionDraft>) => void;
  owner: boolean;
  finding: Finding | null;
  errors: FieldErrors;
  disabled?: boolean;
  onChangeKind?: () => void;
  onFixPath: (action: FindingAction) => void;
  onOpenExisting: (serviceId: string) => void;
  onRecheck: () => void;
}) {
  const address = fieldOf(definition, "address");
  const key = fieldOf(definition, "api_key");
  const env = fieldOf(definition, "api_key_env");
  // One finding shows at a time, so one id names its text for whichever control it is about.
  const findingText = useId();
  const card = (field: Finding["field"], context?: string) =>
    finding && finding.field === field ? (
      <FindingCard
        finding={finding}
        textId={findingText}
        context={context}
        onFixPath={onFixPath}
        onOpenExisting={onOpenExisting}
        onRecheck={onRecheck}
      />
    ) : null;
  const describes = (field: Finding["field"]) => (finding?.field === field ? findingText : undefined);

  return (
    <div className="flex flex-col gap-4">
      <div className="bg-muted rounded-control text-meta flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <span className="flex items-center gap-2">
          <Check aria-hidden="true" className="text-success size-4" />
          <b className="font-medium">{definition.title}</b>
        </span>
        {!disabled && onChangeKind && <LinkButton onClick={onChangeKind}>Change kind</LinkButton>}
      </div>
      {address && (
        <FormField
          label={address.label}
          required
          help={address.help}
          error={errors.address}
          extraDescribedBy={describes("address")}
          after={
            <>
              <CallLine definition={definition} values={{ address: draft.address }} />
              {card("address")}
              {finding?.field === "key" && (
                <p className="text-meta text-success flex items-center gap-1.5">
                  <CircleCheck aria-hidden="true" className="size-4" />
                  Reached the server.
                </p>
              )}
            </>
          }
        >
          {(control) => (
            <Input
              {...control}
              aria-invalid={control["aria-invalid"] ?? (finding?.field === "address" && finding.severity === "error" ? true : undefined)}
              type="url"
              placeholder={address.placeholder}
              value={draft.address}
              disabled={disabled}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onChange({ address: event.target.value })}
            />
          )}
        </FormField>
      )}
      {owner && draft.envOpen && env ? (
        <FormField
          label={env.label}
          help={env.help}
          error={errors.api_key_env}
          extraDescribedBy={describes("key")}
          after={
            <>
              {card("key", ENV_KEY_CONTEXT)}
              {!disabled && <LinkButton onClick={() => onChange({ envOpen: false })}>Paste the key here instead</LinkButton>}
            </>
          }
        >
          {(control) => (
            <Input
              {...control}
              aria-invalid={control["aria-invalid"] ?? (finding?.field === "key" ? true : undefined)}
              className="font-mono"
              placeholder={env.placeholder}
              value={draft.envName}
              disabled={disabled}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onChange({ envName: event.target.value })}
            />
          )}
        </FormField>
      ) : (
        key && (
          <FormField
            label={key.label}
            help={owner ? key.help : undefined}
            error={errors.api_key}
            extraDescribedBy={describes("key")}
            tag={owner ? undefined : <OwnerOnlyTag />}
            after={
              <>
                {!owner && (
                  <Note tone="lock">
                    <b className="font-medium">Only the owner can add a key.</b> You can still add a server that needs no key.
                  </Note>
                )}
                {card("key")}
                {owner && !disabled && env && (
                  <LinkButton onClick={() => onChange({ envOpen: true })}>
                    Read the key from a server environment variable instead
                  </LinkButton>
                )}
              </>
            }
          >
            {(control) =>
              owner ? (
                <Input
                  {...control}
                  aria-invalid={control["aria-invalid"] ?? (finding?.field === "key" ? true : undefined)}
                  type="password"
                  autoComplete="new-password"
                  placeholder={key.placeholder}
                  value={draft.key}
                  disabled={disabled}
                  onChange={(event) => onChange({ key: event.target.value })}
                />
              ) : (
                <Input {...control} type="password" placeholder="Owner only" value="" disabled readOnly />
              )
            }
          </FormField>
        )
      )}
    </div>
  );
}

/** The one-line summary above the fields after a check: "1 problem. Fix it, or add the engine turned off." */
export function SummaryLine({ finding }: { finding: Finding }) {
  if (finding.severity === "warning")
    return (
      <p role="status" className="text-meta text-warning flex items-center gap-2 font-medium">
        <CircleAlert aria-hidden="true" className="size-4" />
        The check finished with 1 warning.
      </p>
    );
  return (
    <p className="text-meta text-danger flex items-center gap-2 font-medium">
      <CircleX aria-hidden="true" className="size-4" />
      {finding.summary ?? (finding.keep_off ? "1 problem. Fix it, or add the engine turned off." : "1 problem to fix before you continue.")}
    </p>
  );
}

