"use client";
import { useRef, useState } from "react";
import { Info, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import type {
  PlanRef,
  RuleField,
  SeenValue,
  SignInRule,
  SignInRules,
  SignInSnapshot,
} from "@/lib/signin-api";
import { cn } from "@/lib/cn";
import { Band, Block, LinkButton } from "./bits";
import {
  isComplete,
  letsIn,
  noGroupsRuleNote,
  onlyInvitedNote,
  openFallbackWarning,
  type ProviderLabel,
} from "./copy";
import { moveRule, newRule, planRules, sameRule } from "./form";
import { RuleCard } from "./rule-card";

const FIELD_ORDER: RuleField[] = ["group", "email", "domain", "everyone"];

export function RulesEditor({
  rules,
  saved,
  label,
  plans,
  seen,
  ruleMatches,
  groupsMissingSince,
  showNotice,
  disabled,
  phone,
  onChange,
  onDismissNotice,
}: {
  rules: SignInRules;
  saved: SignInRules;
  label: ProviderLabel;
  plans: PlanRef[];
  seen: SignInSnapshot["seen"];
  ruleMatches: SignInSnapshot["rule_matches"];
  /** "today" or a date, when the provider last sent no groups. */
  groupsMissingSince: string | null;
  showNotice: boolean;
  disabled: boolean;
  phone: boolean;
  onChange: (rules: SignInRules) => void;
  onDismissNotice: () => void;
}) {
  const counter = useRef(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const list = rules.rules;
  const plan = planRules(list);
  const fields = (): RuleField[] =>
    seen.role.length || list.some((r) => r.field === "role")
      ? ["group", "role", "email", "domain", "everyone"]
      : FIELD_ORDER;
  const setRules = (next: SignInRule[]) => onChange({ ...rules, rules: next });
  const anyLetsIn = list.filter(isComplete).some(letsIn);
  const valuesFor = (f: RuleField): SeenValue[] =>
    f === "everyone" ? [] : seen[f];

  return (
    <Block title="Who can sign in and what they get" id="sign-in-rules">
      <div className="border-b p-4">
        <p className="text-body font-medium">Everyone else</p>
        <RadioGroup
          aria-label="Everyone else"
          value={rules.fallback}
          disabled={disabled}
          onValueChange={(v) =>
            onChange({ ...rules, fallback: v as SignInRules["fallback"] })
          }
          className="mt-2.5 grid gap-3 sm:grid-cols-2"
        >
          <FallbackChoice
            value="closed"
            title="Keep them out unless I invite them"
            description="Only the rules below and invitations let people in."
          />
          <FallbackChoice
            value="open"
            title="Let them in, but with no access until I give them a plan"
          />
        </RadioGroup>
      </div>
      {rules.fallback === "open" && (
        <Band tone="warn">{openFallbackWarning(label)}</Band>
      )}
      {rules.fallback === "closed" && !anyLetsIn && (
        <Band tone="info">{onlyInvitedNote(label)}</Band>
      )}
      {showNotice && (
        <Band
          tone="info"
          action={
            <LinkButton onClick={onDismissNotice}>Dismiss</LinkButton>
          }
        >
          Removed a rule that only named you. You are always let in.
        </Band>
      )}
      {groupsMissingSince && list.some((r) => r.field === "group") && (
        <Band tone="warn">{noGroupsRuleNote(label, groupsMissingSince)}</Band>
      )}
      {list.length ? (
        list.map((rule, i) => {
          const savedRule = saved.rules.find((r) => r.id === rule.id);
          const unchanged = savedRule ? sameRule(savedRule, rule) : false;
          const at = plan.findIndex((r) => r.id === rule.id);
          return (
            <RuleCard
              key={rule.id}
              rule={rule}
              position={i + 1}
              fields={fields()}
              seen={valuesFor(rule.field)}
              plans={plans}
              label={label}
              matches={unchanged ? (ruleMatches[rule.id] ?? null) : null}
              isNew={!savedRule}
              planPriority={rule.plan && plan.length > 1 ? at + 1 : null}
              canMoveUp={at > 0}
              canMoveDown={at >= 0 && at < plan.length - 1}
              pickerOpen={openId === rule.id}
              phone={phone}
              disabled={disabled}
              onPickerOpenChange={(open) => setOpenId(open ? rule.id : null)}
              onChange={(next) =>
                setRules(list.map((r) => (r.id === rule.id ? next : r)))
              }
              onFieldChange={(field) => {
                setRules(
                  list.map((r) =>
                    r.id === rule.id ? { ...r, field, values: [] } : r,
                  ),
                );
                setOpenId(field === "everyone" ? null : rule.id);
              }}
              onDelete={() => {
                setRules(list.filter((r) => r.id !== rule.id));
                setOpenId(null);
              }}
              onMove={(direction) =>
                setRules(moveRule(list, rule.id, direction))
              }
            />
          );
        })
      ) : (
        <p className="text-muted-foreground px-4 py-5 text-center">
          No rules yet.
        </p>
      )}
      <div className="flex flex-col items-start gap-2 border-t px-4 py-3">
        {!disabled && (
          <Button
            variant="outline"
            onClick={() => {
              const id = `new-${++counter.current}-${list.length}`;
              setRules([...list, newRule(id)]);
              setOpenId(id);
            }}
          >
            <Plus />
            Add a rule
          </Button>
        )}
        <FixedNote>People you invite can always finish signing in.</FixedNote>
        <FixedNote>
          The first rule that gives a plan wins. A plan you set on People, or
          that an invitation carried, always stays.
        </FixedNote>
      </div>
    </Block>
  );
}

function FixedNote({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-muted-foreground text-meta flex items-start gap-1.5">
      <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span>{children}</span>
    </span>
  );
}

function FallbackChoice({
  value,
  title,
  description,
}: {
  value: string;
  title: string;
  description?: string;
}) {
  const id = `sign-in-fallback-${value}`;
  return (
    <label
      htmlFor={id}
      className={cn(
        "group/choice rounded-panel bg-card hover:border-input flex cursor-pointer items-start gap-2.5 border p-3",
        "has-[[data-state=checked]]:border-ring has-[[data-state=checked]]:bg-accent has-[:disabled]:cursor-not-allowed",
      )}
    >
      <RadioGroupItem value={value} id={id} className="mt-0.5" />
      <span className="block min-w-0">
        <span className="block font-medium">{title}</span>
        {description && (
          <span className="text-muted-foreground text-meta group-has-[[data-state=checked]]/choice:text-accent-foreground mt-0.5 block">
            {description}
          </span>
        )}
      </span>
    </label>
  );
}
