"use client";

/**
 * More options: one collapsed section that opens by itself when anything in
 * it differs from the definition's defaults. Small headings inside, one per
 * field group the definition lists. Typed forms only.
 */
import type { ReactNode } from "react";
import { Plus, Trash2 } from "lucide-react";
import type { EngineDefinition, EngineField, HeaderRow } from "@/lib/engines-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CallLine } from "../parts/call-line";
import { FormField, NativeSelect, Note, OwnerOnlyTag, Tag } from "../parts/form";
import { transformsFromCards, type AdjustmentCard } from "./adjustments";
import { AdjustmentsEditor } from "./adjustment-forms";

/** A header row while it is being typed. Values are plain text. */
export type HeaderDraft = HeaderRow;

/** The plain values More options edits, as text while they are being typed. */
export type OptionValues = Record<string, string>;

export function headersFrom(rows: HeaderRow[] | undefined): HeaderDraft[] {
  return (rows ?? []).map((h) => ({ name: h.name, value: h.value }));
}

export function headersBody(rows: HeaderDraft[]): HeaderRow[] {
  return rows.map((h) => ({ name: h.name, value: h.value }));
}

function unitWord(unit: EngineField["unit"]): string {
  return unit === "minutes" ? "minutes" : unit === "tokens" ? "tokens" : "seconds";
}
function unitShort(unit: EngineField["unit"]): string {
  return unit === "minutes" ? " min" : unit === "tokens" ? "" : " s";
}

export function optionFields(definition: EngineDefinition): EngineField[] {
  return definition.fields.filter((f) => f.advanced && f.group).sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

function defaultText(field: EngineField): string {
  return field.default === undefined || field.default === null ? "" : String(field.default);
}

/** How many settings differ from the definition's defaults. */
export function changedCount(definition: EngineDefinition, values: OptionValues, headers: HeaderDraft[], cards: AdjustmentCard[]): number {
  let n = 0;
  for (const field of optionFields(definition)) {
    if (field.show_if) continue;
    if (field.type === "keyvalue") n += headers.length ? 1 : 0;
    else if (field.items === "transform") {
      const now = transformsFromCards(cards).map((row) => JSON.stringify(row));
      const was = (Array.isArray(field.default) ? field.default : []).map((row) => JSON.stringify(row));
      if (now.join("\n") !== was.join("\n")) {
        // Each added or removed adjustment counts once.
        const added = now.filter((row) => !was.includes(row)).length;
        const removed = was.filter((row) => !now.includes(row)).length;
        n += Math.max(1, added + removed);
      }
    } else if (field.type === "duration" || field.type === "number") {
      if (Number(values[field.key]) !== Number(field.default ?? 0)) n += 1;
    } else if ((values[field.key] ?? "") !== defaultText(field)) n += 1;
  }
  return n;
}

function visible(field: EngineField, values: OptionValues): boolean {
  return !field.show_if || values[field.show_if.field] === String(field.show_if.equals);
}

export function MoreOptions({
  definition,
  values,
  onValues,
  headers,
  onHeaders,
  cards,
  onCards,
  owner,
  lifecycleEditable,
  locked,
  address,
  open,
  onOpenChange,
}: {
  definition: EngineDefinition;
  values: OptionValues;
  onValues: (patch: OptionValues) => void;
  headers: HeaderDraft[];
  onHeaders: (rows: HeaderDraft[]) => void;
  cards: AdjustmentCard[];
  onCards: (cards: AdjustmentCard[]) => void;
  owner: boolean;
  lifecycleEditable: boolean;
  locked: boolean;
  address: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const fields = optionFields(definition);
  const groups: { name: string; fields: EngineField[] }[] = [];
  for (const field of fields) {
    const group = groups.find((g) => g.name === field.group);
    if (group) group.fields.push(field);
    else groups.push({ name: field.group as string, fields: [field] });
  }
  const n = changedCount(definition, values, headers, cards);

  return (
    <details
      id="engine-more"
      open={open}
      onToggle={(event) => onOpenChange(event.currentTarget.open)}
      className="bg-card rounded-panel scroll-mt-20 border"
    >
      <summary className="flex min-h-12 cursor-pointer flex-wrap items-center justify-between gap-2 px-4 py-3">
        <span className="text-heading">More options</span>
        {n ? <Tag tone="changed">{n} changed from default</Tag> : <Tag>All defaults</Tag>}
      </summary>
      <div className="flex flex-col divide-y border-t">
        {groups.map((group) => {
          const ownerOnly = group.fields.some((f) => f.owner_only);
          const id = `engine-group-${group.name.toLowerCase().replace(/[^a-z]+/g, "-")}`;
          return (
            <section key={group.name} aria-labelledby={id} className="flex flex-col gap-3 p-4">
              <h4 id={id} className="text-body flex items-center gap-2 font-medium">
                {group.name}
                {ownerOnly && <OwnerOnlyTag />}
              </h4>
              <GroupBody
                group={group.fields}
                values={values}
                onValues={onValues}
                headers={headers}
                onHeaders={onHeaders}
                cards={cards}
                onCards={onCards}
                owner={owner}
                lifecycleEditable={lifecycleEditable}
                locked={locked}
                address={address}
                definition={definition}
              />
            </section>
          );
        })}
      </div>
    </details>
  );
}

function GroupBody({
  group,
  values,
  onValues,
  headers,
  onHeaders,
  cards,
  onCards,
  owner,
  lifecycleEditable,
  locked,
  address,
  definition,
}: {
  group: EngineField[];
  values: OptionValues;
  onValues: (patch: OptionValues) => void;
  headers: HeaderDraft[];
  onHeaders: (rows: HeaderDraft[]) => void;
  cards: AdjustmentCard[];
  onCards: (cards: AdjustmentCard[]) => void;
  owner: boolean;
  lifecycleEditable: boolean;
  locked: boolean;
  address: string;
  definition: EngineDefinition;
}) {
  const ownerOnly = group.some((f) => f.owner_only);
  if (ownerOnly) return <OwnerOnlyGroup fields={group} values={values} onValues={onValues} editable={owner && lifecycleEditable && !locked} />;
  const out: ReactNode[] = [];
  const plain: EngineField[] = [];
  for (const field of group) {
    if (field.type === "keyvalue")
      out.push(<HeadersEditor key={field.key} field={field} rows={headers} onChange={onHeaders} disabled={locked} />);
    else if (field.items === "transform")
      out.push(
        <div key={field.key} className="flex flex-col gap-3">
          {field.help && <p className="text-meta text-muted-foreground">{field.help}</p>}
          <AdjustmentsEditor cards={cards} onChange={onCards} disabled={locked} />
        </div>,
      );
    else plain.push(field);
  }
  if (plain.length)
    out.push(
      <div key="plain" className="grid gap-3 sm:grid-cols-2">
        {plain.map((field) => (
          <PlainField
            key={field.key}
            field={field}
            value={values[field.key] ?? ""}
            onChange={(v) => onValues({ [field.key]: v })}
            disabled={locked}
            after={field.key === "path_prefix" ? <CallLine definition={definition} values={{ address, path_prefix: values.path_prefix }} /> : undefined}
          />
        ))}
      </div>,
    );
  return <>{out}</>;
}

function PlainField({
  field,
  value,
  onChange,
  disabled,
  after,
  withDefaultTag = true,
}: {
  field: EngineField;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  after?: ReactNode;
  withDefaultTag?: boolean;
}) {
  const numeric = field.type === "duration" || field.type === "number";
  const changed = numeric ? Number(value) !== Number(field.default ?? 0) : value !== defaultText(field);
  const tag =
    numeric && withDefaultTag && field.default !== undefined ? (
      changed ? (
        <Tag tone="changed">{`Changed · default ${defaultText(field)}${unitShort(field.unit)}`}</Tag>
      ) : (
        <Tag>{`Default ${defaultText(field)}${unitShort(field.unit)}`}</Tag>
      )
    ) : undefined;
  if (field.type === "select")
    return (
      <FormField label={field.label} help={field.help} tag={changed ? <Tag tone="changed">Changed</Tag> : <Tag>Default</Tag>}>
        {(control) => (
          <NativeSelect {...control} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
            {(field.options ?? []).map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </NativeSelect>
        )}
      </FormField>
    );
  return (
    <FormField label={field.label} help={field.help} tag={tag} after={after}>
      {(control) =>
        numeric ? (
          <div className="flex items-center gap-2">
            <Input
              {...control}
              inputMode="numeric"
              value={value}
              disabled={disabled}
              autoComplete="off"
              onChange={(event) => onChange(event.target.value)}
            />
            <span className="text-meta text-muted-foreground">{unitWord(field.unit)}</span>
          </div>
        ) : (
          <Input
            {...control}
            className="font-mono"
            placeholder={field.placeholder}
            value={value}
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
          />
        )
      }
    </FormField>
  );
}

/** Start and stop: owner only. Typed fields for the owner, the lock text for everyone else. */
function OwnerOnlyGroup({
  fields,
  values,
  onValues,
  editable,
}: {
  fields: EngineField[];
  values: OptionValues;
  onValues: (patch: OptionValues) => void;
  editable: boolean;
}) {
  const mode = fields.find((f) => f.type === "select");
  const managed = mode ? values[mode.key] !== defaultText(mode) : false;
  const shown = fields.filter((f) => f !== mode && visible(f, values));
  if (!editable) {
    return (
      <>
        <p className="text-meta text-muted-foreground">Only needed when Azizam should start this server itself.</p>
        <Note tone="lock">
          Only the owner can change this.{" "}
          {managed ? "Azizam starts and stops this server with the commands below." : "Azizam leaves this server running."}
        </Note>
        {managed && (
          <dl className="text-meta grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {shown.map((f) => (
              <div key={f.key} className="contents">
                <dt className="text-muted-foreground">{f.label}</dt>
                <dd className="font-mono [overflow-wrap:anywhere]">
                  {values[f.key] || "None"}
                  {f.type === "duration" && values[f.key] ? ` ${unitWord(f.unit)}` : ""}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </>
    );
  }
  const text = shown.filter((f) => f.type !== "duration");
  const durations = shown.filter((f) => f.type === "duration");
  return (
    <>
      <p className="text-meta text-muted-foreground">Only needed when Azizam should start this server itself.</p>
      {mode && <PlainField field={mode} value={values[mode.key] ?? defaultText(mode)} onChange={(v) => onValues({ [mode.key]: v })} />}
      {text.map((f) => (
        <PlainField key={f.key} field={f} value={values[f.key] ?? ""} onChange={(v) => onValues({ [f.key]: v })} />
      ))}
      {durations.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {durations.map((f) => (
            <PlainField key={f.key} field={f} value={values[f.key] ?? ""} withDefaultTag={false} onChange={(v) => onValues({ [f.key]: v })} />
          ))}
        </div>
      )}
      {managed && (
        <Note tone="info">
          Azizam starts this server when it is needed, so it cannot check it now. It saves as <b>Not started; checked on first start</b>.
        </Note>
      )}
    </>
  );
}

function HeadersEditor({
  field,
  rows,
  onChange,
  disabled,
}: {
  field: EngineField;
  rows: HeaderDraft[];
  onChange: (rows: HeaderDraft[]) => void;
  disabled?: boolean;
}) {
  const set = (i: number, patch: Partial<HeaderDraft>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-3">
      {field.help && <p className="text-meta text-muted-foreground">{field.help}</p>}
      {rows.map((row, i) => (
        <div key={i} className="grid items-end gap-2 sm:grid-cols-[1fr_1fr_auto]">
          <FormField label="Header name">
            {(control) => (
              <Input
                {...control}
                size="sm"
                className="font-mono"
                placeholder="X-Client"
                value={row.name}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => set(i, { name: event.target.value })}
              />
            )}
          </FormField>
          <FormField label="Value">
            {(control) => (
              <Input
                {...control}
                size="sm"
                className="font-mono"
                value={row.value}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => set(i, { value: event.target.value })}
              />
            )}
          </FormField>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove header ${row.name || i + 1}`}
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            <Trash2 aria-hidden="true" />
          </Button>
        </div>
      ))}
      {!disabled && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="w-fit"
          onClick={() => onChange([...rows, { name: "", value: "" }])}
        >
          <Plus aria-hidden="true" />
          Add a header
        </Button>
      )}
    </div>
  );
}
