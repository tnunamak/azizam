"use client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormField, Tag } from "../parts/form";
import type { SecretDraft, SecretRow } from "./secret-edits";
let nextRow = 0;
export function SecretFields({
  drafts,
  onChange,
  part,
  disabled,
  excludeKinds = [],
}: {
  drafts: SecretDraft[];
  onChange: (drafts: SecretDraft[]) => void;
  part: "connection" | "process" | "all";
  disabled: boolean;
  excludeKinds?: SecretDraft["kind"][];
}) {
  const change = (carrier: string, rows: SecretRow[]) =>
    onChange(drafts.map((d) => (d.carrier === carrier ? { ...d, rows } : d)));
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {drafts
        .filter(
          (d) =>
            !excludeKinds.includes(d.kind) &&
            (part === "all" ||
              (part === "connection"
                ? ["headers", "url", "key"].includes(d.kind)
                : ["env", "argv"].includes(d.kind))),
        )
        .map((draft) => {
          const set = (key: string, patch: Partial<SecretRow>) =>
            change(
              draft.carrier,
              draft.rows.map((r) => (r.key === key ? { ...r, ...patch } : r)),
            );
          const rows = draft.rows.filter((r) => !r.removed);
          const command = draft.kind === "argv";
          const named = draft.kind === "headers" || draft.kind === "env";
          return (
            <fieldset
              key={draft.carrier}
              disabled={disabled}
              className="bg-card rounded-panel flex min-w-0 flex-col gap-3 border p-4"
            >
              <legend className="text-body px-1 font-medium [overflow-wrap:anywhere]">
                {draft.label}
              </legend>
              {draft.kind === "headers" && (
                <p className="text-meta text-muted-foreground">
                  Header values are stored as secrets. Enter a replacement or
                  remove a row. Unchanged entries keep their configured values.
                </p>
              )}
              {draft.kind === "env" && (
                <p className="text-meta text-muted-foreground">
                  Environment values are write-only secrets. To rename a
                  variable, remove it and add the new variable.
                </p>
              )}
              {command && (
                <p className="text-meta text-muted-foreground">
                  The first row is the program. Each remaining row is one
                  argument, including any spaces. Shell quoting and expansion do
                  not run.
                </p>
              )}
              {draft.kind === "key" && (
                <p className="text-meta text-muted-foreground">
                  The key is write-only. A replacement removes any
                  environment-variable binding. Clear the key to remove its
                  stored value or binding.
                </p>
              )}
              {draft.kind === "url" && (
                <p className="text-meta text-muted-foreground">
                  To replace URL credentials, enter the complete URL with its
                  new credentials. The saved endpoint stays public; credentials
                  are write-only.
                </p>
              )}
              {rows.map((r, index) => {
                const label = command
                  ? `${draft.label} ${index === 0 ? "Program" : `Argument ${index}`}`
                  : draft.kind === "key"
                    ? draft.carrier === "config.api_key"
                      ? "API key"
                      : `${draft.carrier} key`
                    : draft.kind === "url"
                      ? `${draft.carrier} replacement URL`
                      : `Value for ${r.name || "new entry"}`;
                const removalLabel =
                  draft.kind === "headers"
                    ? `Remove header ${r.name || index + 1}`
                    : draft.kind === "env"
                      ? `Remove variable ${r.name || index + 1}`
                      : draft.kind === "key"
                        ? draft.carrier === "config.api_key"
                          ? "Clear API key"
                          : `Clear ${draft.carrier} key`
                        : draft.kind === "url"
                          ? `Clear ${draft.carrier} credentials`
                          : `Remove ${label}`;
                return (
                  <div
                    key={r.key}
                    className="flex min-w-0 flex-col gap-2 border-b pb-3 last:border-b-0"
                  >
                    {named && (
                      <FormField
                        label={
                          draft.kind === "headers"
                            ? "Header name"
                            : "Variable name"
                        }
                      >
                        {(control) => (
                          <Input
                            {...control}
                            value={r.name}
                            readOnly={draft.kind === "env" && !!r.id}
                            autoComplete="off"
                            onChange={(e) =>
                              set(r.key, { name: e.target.value })
                            }
                          />
                        )}
                      </FormField>
                    )}
                    <FormField
                      label={label}
                      tag={r.configured ? <Tag>Configured</Tag> : undefined}
                    >
                      {(control) => (
                        <Input
                          {...control}
                          type={r.secret ? "password" : "text"}
                          value={r.value}
                          autoComplete="off"
                          spellCheck={false}
                          onChange={(e) =>
                            set(r.key, { value: e.target.value, edited: true })
                          }
                        />
                      )}
                    </FormField>
                    {r.prefix && (
                      <p className="text-meta text-muted-foreground break-all">
                        Argument prefix: <code>{r.prefix}</code>
                      </p>
                    )}
                    <div className="flex flex-wrap items-center gap-3">
                      {command && (
                        <label className="text-meta flex min-h-11 items-center gap-2">
                          <input
                            type="checkbox"
                            aria-label={`Secret ${label}`}
                            checked={r.secret}
                            disabled={index === 0}
                            onChange={(e) =>
                              set(r.key, {
                                secret: e.target.checked,
                                edited: true,
                              })
                            }
                          />
                          Secret
                        </label>
                      )}
                      {(r.edited || (!!r.value && r.secret)) && (
                        <span className="text-meta">
                          {r.id || r.configured
                            ? "Replacement pending"
                            : "New value pending"}
                        </span>
                      )}
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={removalLabel}
                        onClick={() => set(r.key, { removed: true })}
                      >
                        {draft.kind === "key"
                          ? "Clear key"
                          : draft.kind === "url"
                            ? "Clear credentials"
                            : "Remove"}
                      </Button>
                      {r.edited && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            (() => {
                              const original = (
                                JSON.parse(draft.initial) as SecretRow[]
                              ).find((row) => row.key === r.key);
                              change(
                                draft.carrier,
                                original
                                  ? draft.rows.map((row) =>
                                      row.key === r.key ? original : row,
                                    )
                                  : draft.rows.filter(
                                      (row) => row.key !== r.key,
                                    ),
                              );
                            })()
                          }
                        >
                          Cancel replacement
                        </Button>
                      )}
                    </div>
                  </div>
                );
              })}
              {draft.rows
                .filter((r) => r.removed)
                .map((r) => (
                  <div
                    key={r.key}
                    className="text-meta flex flex-wrap items-center gap-2"
                  >
                    <span>Removal pending{r.name ? `: ${r.name}` : ""}.</span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => set(r.key, { removed: undefined })}
                    >
                      Undo removal
                    </Button>
                  </div>
                ))}
              {draft.kind !== "url" && draft.kind !== "key" && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="w-fit"
                  aria-label={
                    command
                      ? `Add ${draft.label} argument`
                      : draft.kind === "headers"
                        ? "Add a header"
                        : `Add ${draft.label} variable`
                  }
                  onClick={() =>
                    change(draft.carrier, [
                      ...draft.rows,
                      {
                        key: `new-${++nextRow}`,
                        name: "",
                        originalName: "",
                        value: "",
                        secret: !command,
                      },
                    ])
                  }
                >
                  {command
                    ? "Add an argument"
                    : draft.kind === "headers"
                      ? "Add a header"
                      : "Add a variable"}
                </Button>
              )}
            </fieldset>
          );
        })}
    </div>
  );
}
