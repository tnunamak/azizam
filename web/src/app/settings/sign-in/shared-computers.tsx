"use client";
import { useId } from "react";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import type { SharedComputersMode } from "@/lib/signin-api";
import { useReadOnly } from "./bits";
import { SHARED_COMPUTERS } from "./copy";

const HEADING_ID = "sign-in-shared";

/**
 * On shared computers (S12, DESIGN.md section 8): whether the sign-in page keeps the names of people who signed in on a
 * browser. Each choice is a card of its own, and the chosen one takes the primary border. The choice is saved with the
 * other sign-in settings, so it takes effect on Save.
 */
export function SharedComputers({
  value,
  onChange,
}: {
  value: SharedComputersMode;
  onChange: (value: SharedComputersMode) => void;
}) {
  const readOnly = useReadOnly();
  const base = useId();
  const rememberId = `${base}-remember`;
  const nobodyId = `${base}-nobody`;
  return (
    <section aria-labelledby={HEADING_ID} className="min-w-0">
      <div className="mb-(--space-heading)">
        <h3 id={HEADING_ID} className="text-heading">
          {SHARED_COMPUTERS.heading}
        </h3>
        <p className="text-muted-foreground text-meta">{SHARED_COMPUTERS.help}</p>
      </div>
      <RadioGroup
        aria-labelledby={HEADING_ID}
        value={value}
        onValueChange={(next) => onChange(next as SharedComputersMode)}
        disabled={readOnly}
        className="grid gap-2"
      >
        <div className="bg-card rounded-control flex items-start gap-3 border px-4 py-3 has-data-[state=checked]:border-primary">
          <RadioGroupItem
            id={rememberId}
            value="remember"
            aria-describedby={`${rememberId}-help`}
            className="mt-1"
          />
          <div className="min-w-0 flex-1 space-y-1">
            <Label htmlFor={rememberId} className="text-body font-medium">
              {SHARED_COMPUTERS.remember.label}
            </Label>
            <p id={`${rememberId}-help`} className="text-muted-foreground text-meta">
              {SHARED_COMPUTERS.remember.help}
            </p>
          </div>
        </div>
        <div className="bg-card rounded-control flex items-start gap-3 border px-4 py-3 has-data-[state=checked]:border-primary">
          <RadioGroupItem
            id={nobodyId}
            value="nobody"
            aria-describedby={`${nobodyId}-help`}
            className="mt-1"
          />
          <div className="min-w-0 flex-1 space-y-1">
            <Label htmlFor={nobodyId} className="text-body font-medium">
              {SHARED_COMPUTERS.nobody.label}
            </Label>
            <p id={`${nobodyId}-help`} className="text-muted-foreground text-meta">
              {SHARED_COMPUTERS.nobody.help}
            </p>
            {value === "nobody" && (
              <p className="text-warning text-meta">{SHARED_COMPUTERS.nobody.forgets}</p>
            )}
          </div>
        </div>
      </RadioGroup>
    </section>
  );
}
