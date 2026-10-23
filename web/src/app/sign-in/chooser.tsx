"use client";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronRight, CircleCheck, Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  PersonAvatar,
  generatedAvatarPalettes,
  type AvatarSelection,
  type GeneratedAvatarVariant,
} from "@/components/product/person-avatar";
import { cn } from "@/lib/cn";
import type { RememberedPerson } from "@/lib/remembered-people";
import type { LeftPerson, Status } from "./sign-in-status";

/** The avatar's colour, as the gateway names it. Never the provider's picture: this list is seen by whoever is at the computer. */
function selectionOf(person: RememberedPerson): AvatarSelection | null {
  const variant = generatedAvatarPalettes.find((entry) => entry.id === person.avatar.variant)?.id;
  return variant ? { kind: "generated", variant: variant as GeneratedAvatarVariant } : null;
}

/** The line under a name: the start of the address when two people share a name, then the provider for single sign-on. */
function secondLine(person: RememberedPerson, provider: string): string {
  return [person.hint, person.method === "sso" ? person.provider ?? provider : null]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
}

/**
 * The status line above a screen (DESIGN.md 4.5; the mockup's `.status`): what just happened, in a success-tint box,
 * with the one offer that goes with it. The offer sits on a line of its own, left-aligned, and is a 44 px target.
 */
export function StatusLine({
  status,
  onForget,
  className,
}: {
  status: Status;
  onForget?: (person: LeftPerson) => void;
  /** The space below the box. The list leaves 24 px; forget mode's reserved space already provides its own. */
  className?: string;
}) {
  return (
    <div
      role="status"
      className={cn(
        "bg-success-tint rounded-control text-body mb-6 flex items-start gap-2.5 px-3.5 py-3",
        className,
      )}
    >
      <CircleCheck aria-hidden className="text-success mt-0.5 size-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p>{status.text}</p>
        {status.forget && onForget && (
          <button
            type="button"
            className="text-link flex w-fit min-h-11 items-center text-left underline underline-offset-4"
            onClick={() => onForget(status.forget!)}
          >
            Forget {status.forget.name} on this browser
          </button>
        )}
      </div>
    </div>
  );
}

type ChooserProps = {
  /** The people to offer, in the order they are shown (the one who just left comes last). */
  people: RememberedPerson[];
  providerName: string;
  status: Status | null;
  /** The tile of a single sign-on person whose provider is opening, or null. */
  opening: string | null;
  alerts: ReactNode;
  onChoose: (person: RememberedPerson) => void;
  onSomeoneElse: () => void;
  onForgetMode: () => void;
  onForgetFromStatus: (person: LeftPerson) => void;
};

/** Who’s signing in? (S4): the people this browser remembers, and the way to someone who is not on the list. */
export function Chooser({
  people,
  providerName,
  status,
  opening,
  alerts,
  onChoose,
  onSomeoneElse,
  onForgetMode,
  onForgetFromStatus,
}: ChooserProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const base = useId();
  const busy = opening !== null;
  const openingPerson = people.find((person) => person.tile === opening);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <>
      {status && <StatusLine status={status} onForget={onForgetFromStatus} />}
      <h1
        ref={headingRef}
        tabIndex={-1}
        id="sign-in-title"
        className="text-title font-semibold tracking-tight outline-none"
      >
        Who’s signing in?
      </h1>
      <div className="mt-4 empty:hidden">{alerts}</div>
      <ul className="mt-6 space-y-2">
        {people.map((person, index) => {
          const isOpening = opening === person.tile;
          const disabled = busy && !isOpening;
          const sub = isOpening ? `Opening ${person.provider ?? providerName}…` : secondLine(person, providerName);
          const nameId = `${base}-name-${index}`;
          const subId = `${base}-sub-${index}`;
          return (
            <li key={person.tile}>
              <button
                type="button"
                aria-labelledby={nameId}
                aria-describedby={sub ? subId : undefined}
                aria-disabled={disabled || undefined}
                tabIndex={disabled ? -1 : undefined}
                onClick={() => {
                  if (!busy) onChoose(person);
                }}
                className="border-border hover:bg-accent flex min-h-[60px] w-full items-center gap-3 rounded-control border p-3 text-left disabled:opacity-50 aria-disabled:opacity-50"
              >
                <PersonAvatar name={person.name} selection={selectionOf(person)} decorative />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span id={nameId} className="text-body truncate font-medium">
                    {person.name}
                  </span>
                  {sub && (
                    <span id={subId} className="text-muted-foreground text-meta truncate">
                      {sub}
                    </span>
                  )}
                </span>
                {isOpening ? (
                  <Loader2
                    aria-hidden
                    className="size-4 shrink-0 motion-safe:animate-spin motion-reduce:hidden"
                  />
                ) : (
                  <ChevronRight aria-hidden className="text-muted-foreground size-4 shrink-0" />
                )}
              </button>
            </li>
          );
        })}
        <li>
          <button
            type="button"
            aria-labelledby={`${base}-else`}
            aria-disabled={busy || undefined}
            tabIndex={busy ? -1 : undefined}
            onClick={() => {
              if (!busy) onSomeoneElse();
            }}
            className="border-border hover:bg-accent flex min-h-[60px] w-full items-center gap-3 rounded-control border p-3 text-left aria-disabled:opacity-50"
          >
            <span
              aria-hidden
              className="border-border flex size-8 shrink-0 items-center justify-center rounded-full border"
            >
              <Plus className="size-4" />
            </span>
            <span id={`${base}-else`} className="text-body flex-1 font-medium">
              Someone else
            </span>
            <ChevronRight aria-hidden className="text-muted-foreground size-4 shrink-0" />
          </button>
        </li>
      </ul>
      {openingPerson && (
        // Only while a provider is opening: the status line above is cleared by the same action, so there is never a
        // second status region on screen.
        <p role="status" className="sr-only">
          Opening {openingPerson.provider ?? providerName}
        </p>
      )}
      <div className="mt-6">
        <Button
          variant="link"
          className="min-h-11 px-0"
          aria-disabled={busy || undefined}
          tabIndex={busy ? -1 : undefined}
          onClick={() => {
            if (!busy) onForgetMode();
          }}
        >
          Forget someone on this browser
        </Button>
      </div>
    </>
  );
}

type ForgetListProps = {
  people: RememberedPerson[];
  providerName: string;
  /** The box that reports the last forget, or null. The space for it is always there. */
  notice: Status | null;
  error: string | null;
  onForget: (person: RememberedPerson) => void;
  onDone: () => void;
};

/**
 * Forget someone on this browser (S5). The names are not buttons here: only Forget is, so choosing a person and
 * removing one stay apart. After a forget, focus goes to the next name's Forget, or to Done when none follows.
 */
export function ForgetList({ people, providerName, notice, error, onForget, onDone }: ForgetListProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const doneRef = useRef<HTMLButtonElement>(null);
  const forgetRefs = useRef(new Map<string, HTMLButtonElement>());
  const [focusNext, setFocusNext] = useState<string | null>(null);
  const base = useId();
  useEffect(() => {
    headingRef.current?.focus();
  }, []);
  useEffect(() => {
    // Runs after the removal has rendered, so the next button exists to receive focus.
    if (focusNext === null) return;
    if (focusNext === "done") doneRef.current?.focus();
    else forgetRefs.current.get(focusNext)?.focus();
    setFocusNext(null);
  }, [focusNext, people]);

  return (
    <>
      <h1
        ref={headingRef}
        tabIndex={-1}
        id="sign-in-title"
        className="text-title font-semibold tracking-tight outline-none"
      >
        Forget someone on this browser
      </h1>
      <p className="text-muted-foreground text-body mt-2">
        Only their name comes off this list. They can still sign in.
      </p>
      {error && (
        <p role="alert" className="text-danger text-body mt-4">
          {error}
        </p>
      )}
      <ul className="mt-6 space-y-2">
        {people.map((person, index) => {
          const sub = secondLine(person, providerName);
          const nameId = `${base}-name-${index}`;
          return (
            <li
              key={person.tile}
              className="border-border flex min-h-[60px] items-center gap-3 rounded-control border p-3"
            >
              <PersonAvatar name={person.name} selection={selectionOf(person)} decorative />
              <span className="flex min-w-0 flex-1 flex-col">
                <span id={nameId} className="text-body truncate font-medium">
                  {person.name}
                </span>
                {sub && <span className="text-muted-foreground text-meta truncate">{sub}</span>}
              </span>
              <Button
                variant="outline"
                className="min-h-11 shrink-0"
                aria-label={`Forget ${person.name} on this browser`}
                ref={(element) => {
                  if (element) forgetRefs.current.set(person.tile, element);
                  else forgetRefs.current.delete(person.tile);
                }}
                onClick={() => {
                  setFocusNext(people[index + 1]?.tile ?? "done");
                  onForget(person);
                }}
              >
                Forget
              </Button>
            </li>
          );
        })}
      </ul>
      {/* The space is there before any forget, so the rows above and Done never move under the pointer. */}
      <div className="mt-4 min-h-[52px]">
        {notice ? (
          <StatusLine status={notice} className="mb-0" />
        ) : (
          <div aria-hidden className="invisible bg-success-tint rounded-control text-body px-3.5 py-3">
            &nbsp;
          </div>
        )}
      </div>
      <div className="mt-2">
        <Button ref={doneRef} className="min-h-11" onClick={onDone}>
          Done
        </Button>
      </div>
    </>
  );
}
