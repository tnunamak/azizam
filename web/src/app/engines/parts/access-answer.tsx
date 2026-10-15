"use client";

/**
 * "Who can use these models": the gateway's own answer, computed from the real
 * access plans. A sentence, the plans that include the models (each a link to
 * its editor) with who holds them, and a link to the plans. It is an answer to
 * read, not a setting: there is nothing to tick.
 */
import { useEffect, useId, useState } from "react";
import Link from "next/link";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { enginesApi, type AccessModel, type EngineAccess, type EngineAccessPlan } from "@/lib/engines-api";
import { Button } from "@/components/ui/button";
import { Tag } from "./form";

const LINK_CLASS = "text-link text-meta inline-flex items-center font-medium underline-offset-4 hover:underline max-sm:min-h-11";

/**
 * The turned-on models as the access question names them: the public name, with
 * the work the check found the model does when it found any ("Embeddings" →
 * embeddings), so a plan for chat is not counted for an embedding model.
 */
export function accessModels(
  models: { id: string; public_name: string; use: string; on: boolean }[],
  capabilities: { id: string; label: string }[],
): AccessModel[] {
  return models
    .filter((m) => m.on)
    .map((m) => {
      const name = m.public_name.trim() || m.id;
      const words = m.use.split(", ");
      const known = capabilities.filter((c) => words.includes(c.label)).map((c) => c.id);
      return known.length ? { name, capabilities: known } : name;
    });
}

/** "3 people and 1 app", "1 person", or "Nobody holds this plan yet". */
export function holdersText(plan: Pick<EngineAccessPlan, "people" | "apps">): string {
  const parts: string[] = [];
  if (plan.people > 0) parts.push(`${plan.people} ${plan.people === 1 ? "person" : "people"}`);
  if (plan.apps > 0) parts.push(`${plan.apps} ${plan.apps === 1 ? "app" : "apps"}`);
  return parts.length ? `Held by ${parts.join(" and ")}` : "Nobody holds this plan yet";
}

function unreadableText(count: number): string {
  return count === 1
    ? "1 access plan could not be read, so it includes none of these."
    : `${count} access plans could not be read, so they include none of these.`;
}

export function AccessAnswer({
  access,
  loading,
  error,
  onRetry,
  errorText = "Azizam couldn’t read the access plans. You can still add the engine; check who can use it on its page.",
  qualifier,
  newTab,
}: {
  access?: EngineAccess | null;
  loading?: boolean;
  error?: boolean;
  onRetry?: () => void;
  errorText?: string;
  /** Says the answer is for something not saved yet, for example "After you save". */
  qualifier?: string;
  /** Open plan links in a new tab, so a dialog in progress is not lost. */
  newTab?: boolean;
}) {
  const headingId = useId();
  const target = newTab ? { target: "_blank", rel: "noopener" } : {};
  const opens = newTab ? <span className="sr-only"> (opens in a new tab)</span> : null;
  let body;
  if (loading) {
    body = (
      <p role="status" className="text-meta text-muted-foreground flex items-center gap-2">
        <Loader2 aria-hidden="true" className="size-4 animate-spin" />
        Checking the access plans…
      </p>
    );
  } else if (error || !access) {
    body = (
      <div role="alert" className="flex flex-wrap items-center gap-2">
        <p className="text-meta min-w-0 flex-1">{errorText}</p>
        {onRetry && (
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )}
      </div>
    );
  } else {
    body = (
      <>
        <p className="text-meta">{access.sentence}</p>
        {access.plans.length > 0 && (
          <ul aria-label="Plans that include these models" className="bg-card rounded-control divide-y border">
            {access.plans.map((plan) => (
              <li key={plan.id} className="text-meta flex flex-wrap items-center gap-x-3 gap-y-0.5 px-3 py-1.5 max-sm:py-0">
                <Link prefetch={false} href={plan.link} className={LINK_CLASS} {...target}>
                  {plan.name}
                  {opens}
                </Link>
                <span className="text-muted-foreground">{plan.why}</span>
                <span className="text-muted-foreground">{holdersText(plan)}</span>
              </li>
            ))}
          </ul>
        )}
        {access.unreadable_plans > 0 && <p className="text-meta text-warning">{unreadableText(access.unreadable_plans)}</p>}
        <Link prefetch={false} href={access.link} className={`${LINK_CLASS} w-fit`} {...target}>
          Access plans
          {opens}
        </Link>
      </>
    );
  }
  return (
    <section aria-labelledby={headingId} aria-live="polite" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id={headingId} className="text-body font-medium">
          Who can use these models
        </h3>
        {qualifier && <Tag tone="changed">{qualifier}</Tag>}
      </div>
      {body}
    </section>
  );
}

/**
 * Asks the gateway who could use these models and shows the answer. It asks
 * when it mounts and again `debounceMs` after the capabilities or the public
 * names change. It saves nothing.
 */
export function AccessAsk({
  capabilities,
  models,
  debounceMs = 0,
  ...shown
}: {
  capabilities: string[];
  models: AccessModel[];
  debounceMs?: number;
} & Pick<Parameters<typeof AccessAnswer>[0], "errorText" | "qualifier" | "newTab">) {
  const now = JSON.stringify({ capabilities, models });
  const [debounced, setDebounced] = useState(now);
  useEffect(() => {
    if (now === debounced) return;
    const timer = setTimeout(() => setDebounced(now), debounceMs);
    return () => clearTimeout(timer);
  }, [now, debounced, debounceMs]);
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["engineAccess", debounced],
    queryFn: () => enginesApi.access(JSON.parse(debounced) as { capabilities: string[]; models: AccessModel[] }),
    placeholderData: keepPreviousData,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  return <AccessAnswer access={data} loading={isPending} error={isError} onRetry={() => void refetch()} {...shown} />;
}
