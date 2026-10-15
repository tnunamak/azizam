"use client";

/**
 * The typed form of each request adjustment, the sentence that says what it
 * does, and the picker that adds one. No JSON anywhere.
 */
import { useState } from "react";
import { ArrowRight, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormField, NativeSelect } from "../parts/form";
import {
  ADJUSTMENT_KINDS,
  cardProblem,
  kindName,
  newCard,
  sayAdjustment,
  type AdjustmentCard,
  type PairRow,
  type Phase,
} from "./adjustments";

function PairsTable({
  rows,
  left,
  right,
  leftPlaceholder,
  rightPlaceholder,
  disabled,
  onChange,
}: {
  rows: PairRow[];
  left: string;
  right: string;
  leftPlaceholder: string;
  rightPlaceholder: string;
  disabled?: boolean;
  onChange: (rows: PairRow[]) => void;
}) {
  const set = (j: number, patch: Partial<PairRow>) => onChange(rows.map((r, i) => (i === j ? { ...r, ...patch } : r)));
  return (
    <div className="flex flex-col gap-2">
      <div aria-hidden="true" className="text-label text-muted-foreground hidden grid-cols-[1fr_auto_1fr_auto] gap-2 sm:grid">
        <span>{left}</span>
        <span className="w-4" />
        <span>{right}</span>
        <span className="w-8" />
      </div>
      {rows.map((row, j) => (
        <div key={j} className="grid grid-cols-[1fr_auto_1fr_auto] items-center gap-2">
          <Input
            size="sm"
            className="font-mono"
            aria-label={`${left} (row ${j + 1})`}
            placeholder={leftPlaceholder}
            value={row.a}
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => set(j, { a: event.target.value })}
          />
          <ArrowRight aria-hidden="true" className="text-muted-foreground size-4" />
          <Input
            size="sm"
            className="font-mono"
            aria-label={`${right} (row ${j + 1})`}
            placeholder={rightPlaceholder}
            value={row.b}
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => set(j, { b: event.target.value })}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove row ${j + 1}`}
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, i) => i !== j))}
          >
            <Trash2 aria-hidden="true" />
          </Button>
        </div>
      ))}
      {!disabled && (
        <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => onChange([...rows, { a: "", b: "" }])}>
          <Plus aria-hidden="true" />
          Add a row
        </Button>
      )}
    </div>
  );
}

function CardBody({ card, onChange, disabled }: { card: AdjustmentCard; onChange: (card: AdjustmentCard) => void; disabled?: boolean }) {
  switch (card.kind) {
    case "builtin":
    case "other":
      return null;
    case "value-map":
      return (
        <>
          <FormField label="Field" help="The name of the field in the request or reply.">
            {(control) => (
              <Input
                {...control}
                className="font-mono"
                placeholder="reasoning_effort"
                value={card.field}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => onChange({ ...card, field: event.target.value })}
              />
            )}
          </FormField>
          <div>
            <p className="text-meta mb-1.5 font-medium">Values</p>
            <PairsTable
              rows={card.rows}
              left="When the value is"
              right="Send instead"
              leftPlaceholder="xhigh"
              rightPlaceholder="high"
              disabled={disabled}
              onChange={(rows) => onChange({ ...card, rows })}
            />
          </div>
        </>
      );
    case "param-map":
      return (
        <div>
          <p className="text-meta mb-1.5 font-medium">Field names</p>
          <PairsTable
            rows={card.rows}
            left="Field name"
            right="Rename to"
            leftPlaceholder="max_completion_tokens"
            rightPlaceholder="max_tokens"
            disabled={disabled}
            onChange={(rows) => onChange({ ...card, rows })}
          />
        </div>
      );
    case "default-params":
      return (
        <div>
          <p className="text-meta mb-1.5 font-medium">Defaults</p>
          <PairsTable
            rows={card.rows}
            left="Field"
            right="Value when missing"
            leftPlaceholder="top_p"
            rightPlaceholder="0.9"
            disabled={disabled}
            onChange={(rows) => onChange({ ...card, rows })}
          />
        </div>
      );
    case "drop-params":
      return (
        <div className="flex flex-col gap-2">
          <p className="text-meta font-medium">Fields to leave out</p>
          {card.items.map((item, j) => (
            <div key={j} className="flex items-center gap-2">
              <Input
                size="sm"
                className="font-mono"
                aria-label={`Field to remove, item ${j + 1}`}
                placeholder="logit_bias"
                value={item}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => onChange({ ...card, items: card.items.map((x, i) => (i === j ? event.target.value : x)) })}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove ${item || `item ${j + 1}`}`}
                disabled={disabled}
                onClick={() => onChange({ ...card, items: card.items.filter((_, i) => i !== j) })}
              >
                <Trash2 aria-hidden="true" />
              </Button>
            </div>
          ))}
          {!disabled && (
            <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => onChange({ ...card, items: [...card.items, ""] })}>
              <Plus aria-hidden="true" />
              Add a field
            </Button>
          )}
        </div>
      );
    case "message-inject":
      return (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Sent as">
              {(control) => (
                <NativeSelect {...control} value={card.role} disabled={disabled} onChange={(event) => onChange({ ...card, role: event.target.value })}>
                  <option value="system">Instructions</option>
                  <option value="user">A person’s message</option>
                  {!["system", "user"].includes(card.role) && <option value={card.role}>{card.role}</option>}
                </NativeSelect>
              )}
            </FormField>
            <FormField label="Where">
              {(control) => (
                <NativeSelect
                  {...control}
                  value={card.where}
                  disabled={disabled}
                  onChange={(event) => onChange({ ...card, where: event.target.value as "start" | "end" })}
                >
                  <option value="start">At the start</option>
                  <option value="end">At the end</option>
                </NativeSelect>
              )}
            </FormField>
          </div>
          <FormField label="Message text">
            {(control) => (
              <Textarea
                {...control}
                placeholder="Answer in plain language."
                value={card.text}
                disabled={disabled}
                onChange={(event) => onChange({ ...card, text: event.target.value })}
              />
            )}
          </FormField>
        </>
      );
    case "clamp":
      return (
        <>
          <FormField label="Field">
            {(control) => (
              <Input
                {...control}
                className="font-mono"
                placeholder="temperature"
                value={card.field}
                disabled={disabled}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => onChange({ ...card, field: event.target.value })}
              />
            )}
          </FormField>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Minimum">
              {(control) => (
                <Input
                  {...control}
                  inputMode="decimal"
                  placeholder="0"
                  value={card.min}
                  disabled={disabled}
                  onChange={(event) => onChange({ ...card, min: event.target.value })}
                />
              )}
            </FormField>
            <FormField label="Maximum">
              {(control) => (
                <Input
                  {...control}
                  inputMode="decimal"
                  placeholder="1"
                  value={card.max}
                  disabled={disabled}
                  onChange={(event) => onChange({ ...card, max: event.target.value })}
                />
              )}
            </FormField>
          </div>
        </>
      );
  }
}

export function AdjustmentsEditor({
  cards,
  onChange,
  disabled,
}: {
  cards: AdjustmentCard[];
  onChange: (cards: AdjustmentCard[]) => void;
  disabled?: boolean;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const replace = (index: number, card: AdjustmentCard) => onChange(cards.map((c, i) => (i === index ? card : c)));
  return (
    <div className="flex flex-col gap-3">
      {cards.length === 0 && <p className="text-meta text-muted-foreground">No adjustments. Requests go to the server as they arrive.</p>}
      {cards.map((card, index) => {
        const name = kindName(card);
        const readOnly = card.kind === "builtin" || card.kind === "other";
        return (
          <div key={card.key} data-testid="adjustment" className="rounded-control border">
            <div className="bg-muted/40 flex flex-wrap items-center gap-2 border-b px-3 py-2">
              <h5 className="text-meta flex-1 font-medium">{readOnly ? sayAdjustment(card) : name}</h5>
              {"phase" in card && (
                <label className="text-meta text-muted-foreground flex items-center gap-2">
                  <span aria-hidden="true">Applies to</span>
                  <NativeSelect
                    aria-label={`Applies to, ${name}`}
                    className="h-8 w-auto"
                    value={card.phase}
                    disabled={disabled}
                    onChange={(event) => replace(index, { ...card, phase: event.target.value as Phase })}
                  >
                    <option value="request">Requests</option>
                    <option value="response">Replies</option>
                  </NativeSelect>
                </label>
              )}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`Remove adjustment: ${name}`}
                disabled={disabled}
                onClick={() => onChange(cards.filter((_, i) => i !== index))}
              >
                <Trash2 aria-hidden="true" />
              </Button>
            </div>
            {!readOnly && (
              <div className="flex flex-col gap-3 p-3">
                <CardBody card={card} disabled={disabled} onChange={(next) => replace(index, next)} />
                {cardProblem(card) && <p className="text-meta text-danger">{cardProblem(card)}</p>}
                <p role="status" className="bg-info-tint/60 rounded-control text-meta px-3 py-2">
                  {sayAdjustment(card)}
                </p>
              </div>
            )}
          </div>
        );
      })}
      {!disabled && (
        <details open={pickerOpen} onToggle={(event) => setPickerOpen(event.currentTarget.open)}>
          <summary className="text-meta border-border bg-card rounded-control inline-flex h-8 cursor-pointer items-center gap-1.5 border px-3 font-medium max-sm:min-h-11">
            <Plus aria-hidden="true" className="size-4" />
            Add an adjustment
          </summary>
          <div className="bg-card rounded-panel mt-2 grid gap-1 border p-1 sm:grid-cols-2">
            {ADJUSTMENT_KINDS.map((kind) => (
              <button
                key={kind.kind}
                type="button"
                className="hover:bg-muted rounded-control flex flex-col items-start gap-0.5 px-3 py-2 text-left"
                onClick={() => {
                  onChange([...cards, newCard(kind.kind)]);
                  setPickerOpen(false);
                }}
              >
                <b className="text-meta font-medium">{kind.name}</b>
                <span className="text-meta text-muted-foreground">{kind.description}</span>
              </button>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
