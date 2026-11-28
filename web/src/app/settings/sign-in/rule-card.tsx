"use client";
import { useId, useState } from "react";
import { ArrowDown, ArrowUp, ShieldCheck, Trash2, AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/cn";
import type {
  PlanRef,
  RuleField,
  SeenValue,
  SignInRule,
} from "@/lib/signin-api";
import { DisclosureButton } from "./bits";
import {
  adminNote,
  everyoneAdminWarning,
  FIELD_LABEL,
  isComplete,
  mid,
  people,
  type ProviderLabel,
} from "./copy";
import { ValuePicker } from "./value-picker";

const NO_PLAN = "__no_plan";

export interface RuleMatches {
  count: number;
  names: string[];
}

export function RuleCard({
  rule,
  position,
  fields,
  seen,
  plans,
  label,
  matches,
  isNew,
  planPriority,
  canMoveUp,
  canMoveDown,
  pickerOpen,
  phone,
  disabled,
  onPickerOpenChange,
  onChange,
  onFieldChange,
  onDelete,
  onMove,
}: {
  rule: SignInRule;
  /** 1-based, for the group name. */
  position: number;
  fields: RuleField[];
  seen: SeenValue[];
  plans: PlanRef[];
  label: ProviderLabel;
  /** Known only for a saved rule that has not changed. */
  matches: RuleMatches | null;
  isNew: boolean;
  /** 1-based place among plan rules, when there is more than one. */
  planPriority: number | null;
  canMoveUp: boolean;
  canMoveDown: boolean;
  pickerOpen: boolean;
  phone: boolean;
  disabled: boolean;
  onPickerOpenChange: (open: boolean) => void;
  onChange: (rule: SignInRule) => void;
  onFieldChange: (field: RuleField) => void;
  onDelete: () => void;
  onMove: (direction: "up" | "down") => void;
}) {
  const matchId = useId();
  const [showNames, setShowNames] = useToggle();
  const admitted = rule.admit || rule.admin;
  const planName = plans.find((p) => p.id === rule.plan)?.name;
  return (
    <div role="group" aria-label={`Rule ${position}`} className="border-t first:border-t-0">
      <div className="flex items-start gap-2 px-4 pt-3.5 pb-1.5 max-sm:pr-2">
        <div className="flex min-w-0 flex-1 flex-col gap-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted-foreground text-meta w-11 shrink-0 font-medium max-sm:hidden">
              When
            </span>
            <Select
              value={rule.field}
              disabled={disabled}
              onValueChange={(v) => onFieldChange(v as RuleField)}
            >
              <SelectTrigger
                size="sm"
                aria-label="Rule applies to"
                className="max-sm:basis-28"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {fields.map((f) => (
                  <SelectItem key={f} value={f}>
                    {FIELD_LABEL[f]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {rule.field === "everyone" ? (
              <span className="text-muted-foreground text-meta min-w-0 flex-1 basis-56 max-sm:basis-full">
                every person {mid(label)} signs in
              </span>
            ) : (
              <>
                <span className="text-muted-foreground text-meta max-sm:hidden">
                  {rule.values.length > 1 ? "is one of" : "is"}
                </span>
                <div className="min-w-0 flex-1 basis-56 max-sm:basis-full">
                  <ValuePicker
                    field={rule.field}
                    values={rule.values}
                    seen={seen}
                    open={pickerOpen}
                    phone={phone}
                    disabled={disabled}
                    onOpenChange={onPickerOpenChange}
                    onChange={(values) => onChange({ ...rule, values })}
                  />
                </div>
              </>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="text-muted-foreground text-meta w-11 shrink-0 font-medium max-sm:hidden">
              Then
            </span>
            <CheckLabel
              label="Let in"
              checked={admitted}
              disabled={disabled || rule.admin}
              title={rule.admin ? "Administrators are always let in." : undefined}
              onChange={(admit) => onChange({ ...rule, admit })}
            />
            <CheckLabel
              label="Make administrator"
              checked={rule.admin}
              disabled={disabled}
              onChange={(admin) =>
                onChange({ ...rule, admin, admit: admin ? true : rule.admit })
              }
            />
            <Select
              value={rule.plan ?? NO_PLAN}
              disabled={disabled}
              onValueChange={(v) =>
                onChange({ ...rule, plan: v === NO_PLAN ? null : v })
              }
            >
              <SelectTrigger
                size="sm"
                aria-label="Give plan"
                className={cn(
                  "w-49 max-sm:w-auto max-sm:flex-1 max-sm:basis-32",
                  rule.plan &&
                    "bg-accent text-accent-foreground border-ring font-medium",
                )}
              >
                <SelectValue>
                  {planName ? `Give plan: ${planName}` : "Give plan…"}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PLAN}>No plan</SelectItem>
                {plans.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    Give plan: {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {rule.plan && planPriority !== null && !disabled && (
              <span className="ml-auto inline-flex gap-0.5">
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Move up, higher plan priority"
                  title="Move up, higher plan priority"
                  disabled={!canMoveUp}
                  onClick={() => onMove("up")}
                >
                  <ArrowUp />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Move down, lower plan priority"
                  title="Move down, lower plan priority"
                  disabled={!canMoveDown}
                  onClick={() => onMove("down")}
                >
                  <ArrowDown />
                </Button>
              </span>
            )}
          </div>
        </div>
        {!disabled && (
          <Button
            variant="ghost"
            size="icon"
            aria-label="Remove rule"
            onClick={onDelete}
          >
            <Trash2 />
          </Button>
        )}
      </div>
      <div className="flex flex-col gap-2 px-4 pb-3.5 pl-17 max-sm:pl-4">
        <div className="text-muted-foreground text-label flex flex-wrap items-center gap-x-3 gap-y-1.5">
          {!isComplete(rule) ? (
            <span>
              {rule.field !== "everyone" && !rule.values.length
                ? "Choose who this rule is for and what happens."
                : "Choose what happens to them."}
            </span>
          ) : matches ? (
            matches.count === 0 ? (
              <span>Matches nobody who has signed in yet.</span>
            ) : matches.names.length ? (
              <span>
                <DisclosureButton
                  open={showNames}
                  onClick={setShowNames}
                  controls={matchId}
                  className="text-link w-auto font-medium"
                >
                  Matches {people(matches.count)} who{" "}
                  {matches.count === 1 ? "has" : "have"} signed in
                </DisclosureButton>
                {showNames && (
                  <span id={matchId} className="text-foreground mt-1 block pl-5">
                    {matches.names.join(", ")}
                  </span>
                )}
              </span>
            ) : (
              <span>
                Matches {people(matches.count)} who{" "}
                {matches.count === 1 ? "has" : "have"} signed in
              </span>
            )
          ) : null}
          {planPriority !== null && rule.plan && (
            <Badge variant="secondary" className="bg-accent text-accent-foreground">
              Plan priority {planPriority}
            </Badge>
          )}
          {isNew && (
            <Badge variant="state" state="busy">
              New, not saved
            </Badge>
          )}
        </div>
        {rule.admin && (
          <div className="bg-warning-tint text-warning text-label flex items-start gap-2 rounded-control px-2.5 py-2">
            <ShieldCheck aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
            <span>{adminNote(rule)}</span>
          </div>
        )}
        {rule.admin && rule.field === "everyone" && (
          <div
            role="alert"
            className="bg-danger-tint text-danger text-label flex items-start gap-2 rounded-control px-2.5 py-2 font-medium"
          >
            <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
            <span>{everyoneAdminWarning(label)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function CheckLabel({
  label,
  checked,
  disabled,
  title,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled: boolean;
  title?: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label
      title={title}
      className={cn(
        "text-meta inline-flex min-h-8 cursor-pointer items-center gap-2 font-medium max-sm:min-h-11",
        disabled && "text-muted-foreground cursor-not-allowed",
      )}
    >
      <Checkbox
        checked={checked}
        disabled={disabled}
        onCheckedChange={(v) => onChange(v === true)}
      />
      {label}
    </label>
  );
}

function useToggle() {
  const [on, setOn] = useState(false);
  return [on, () => setOn((v) => !v)] as const;
}
