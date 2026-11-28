"use client";
import { CircleCheck, Info } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/cn";
import type { Preview } from "@/lib/signin-api";
import {
  issuerLine,
  keysLine,
  personChange,
  settingSentence,
  type ChangePart,
  type ProviderLabel,
} from "./copy";

const TONE: Record<ChangePart["tone"], string> = {
  neg: "text-danger font-medium",
  pos: "text-success font-medium",
  plain: "",
  arrow: "text-muted-foreground",
};

/**
 * What saving would do, as the server worked it out. The page only words it:
 * who is affected, and whether their admin keys are revoked, are the server's.
 */
export function WhatChanges({
  preview,
  label,
  keep,
  disabled,
  pending,
  onKeep,
}: {
  preview: Preview;
  /** The shown answer is for an older form; a new one is on its way. */
  pending: boolean;
  label: ProviderLabel;
  keep: Record<string, boolean>;
  disabled: boolean;
  onKeep: (personId: string, keep: boolean) => void;
}) {
  const lines = preview.people
    .map((person) => personChange(person, label))
    .filter((c): c is NonNullable<typeof c> => c !== null);
  const hasLines = lines.length > 0 || preview.issuer_change !== null;
  const issuer = preview.issuer_change ? issuerLine(preview.issuer_change) : null;
  return (
    <div
      id="sign-in-changes"
      role="region"
      aria-label="What changes"
      aria-busy={pending}
      className={cn(
        "border-info/30 bg-info-tint rounded-panel scroll-mt-4 border p-4 transition-opacity",
        pending && "opacity-60",
      )}
    >
      <h3 className="text-heading text-info flex items-center gap-2">
        <Info aria-hidden="true" className="size-4" />
        What changes when you save
      </h3>
      {hasLines ? (
        <ul className="mt-2.5 flex flex-col gap-2">
          {issuer && preview.issuer_change && (
            <li className={ITEM}>
              <span className="font-semibold">{issuer.who}</span>{" "}
              <span className="text-danger font-medium">{issuer.what}</span>
              {issuer.rest}
            </li>
          )}
          {lines.map((c) => {
            const kept = keep[c.id] === true;
            return (
              <li key={c.id} className={ITEM}>
                <span className="font-semibold">{c.name}:</span>{" "}
                {c.clauses.map((clause, ci) => (
                  <span key={ci}>
                    {ci > 0 && ", "}
                    {clause.map((part, pi) => (
                      <span key={pi} className={TONE[part.tone]}>
                        {pi > 0 && " "}
                        {part.text}
                      </span>
                    ))}
                  </span>
                ))}
                .
                {c.keys > 0 && (
                  <>
                    <p className="text-muted-foreground text-meta mt-0.5">
                      {keysLine(c.name, c.keys, kept)}
                    </p>
                    <label
                      className={cn(
                        "text-meta mt-1.5 inline-flex min-h-8 cursor-pointer items-center gap-2 font-medium max-sm:min-h-11",
                      )}
                    >
                      <Checkbox
                        checked={kept}
                        disabled={disabled}
                        onCheckedChange={(v) => onKeep(c.id, v === true)}
                      />
                      Keep them working
                    </label>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-meta mt-1.5">
          No one who has signed in before is affected.
        </p>
      )}
      {preview.settings.length > 0 && (
        <p className="text-meta mt-2.5">
          {preview.settings.map((s) => settingSentence(s, label)).join(" ")}
        </p>
      )}
      {!preview.issuer_change && lines.length > 0 && preview.unaffected.length > 0 && (
        <p className="text-muted-foreground text-meta mt-2.5">
          Not affected: {preview.unaffected.join(", ")}.
        </p>
      )}
      <p className="text-success text-meta mt-2.5 flex items-center gap-1.5">
        <CircleCheck aria-hidden="true" className="size-4" />
        You stay the owner.
      </p>
      <p className="text-muted-foreground text-meta mt-2.5">
        People who have not signed in yet are not shown here.
      </p>
    </div>
  );
}

const ITEM = "bg-card rounded-control text-meta border px-3 py-2.5";
