"use client";
import { useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronDown, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import type { RuleField, SeenValue } from "@/lib/signin-api";
import { noneSeen, people, pickerLabel, pickerPlaceholder } from "./copy";

type Field = Exclude<RuleField, "everyone">;

const sameValue = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/**
 * Pick values the provider has already sent, with how many people carry each,
 * or add one nobody has signed in with. A combobox over a multi-select listbox;
 * on a phone the list opens as a sheet at the bottom of the screen.
 */
export function ValuePicker({
  field,
  values,
  seen,
  open,
  phone,
  disabled,
  onOpenChange,
  onChange,
}: {
  field: Field;
  values: string[];
  seen: SeenValue[];
  open: boolean;
  phone: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
  onChange: (values: string[]) => void;
}) {
  const listId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const addInput = useRef<HTMLInputElement>(null);
  const [typed, setTyped] = useState("");
  const flagsUnseen = field === "group" || field === "role";
  const known = (v: string) => seen.some((o) => sameValue(o.value, v));
  const chosen = (v: string) => values.some((x) => sameValue(x, v));
  const extra = values.filter((v) => !known(v));
  const placeholder = pickerPlaceholder(field, phone);

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node))
        onOpenChange(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open, onOpenChange]);

  const toggle = (v: string) =>
    onChange(
      chosen(v) ? values.filter((x) => !sameValue(x, v)) : [...values, v],
    );
  const add = () => {
    const v = typed.trim();
    if (v && !chosen(v)) onChange([...values, v]);
    setTyped("");
    addInput.current?.focus();
  };
  const close = () => {
    onOpenChange(false);
    trigger.current?.focus();
  };
  const moveFocus = (from: HTMLElement | null, delta: 1 | -1 | "first" | "last") => {
    const options = Array.from(
      wrap.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? [],
    );
    if (!options.length) return;
    const at = from ? options.indexOf(from) : -1;
    const next =
      delta === "first"
        ? 0
        : delta === "last"
          ? options.length - 1
          : Math.min(options.length - 1, Math.max(0, at + delta));
    options[next].focus();
  };

  const options = [
    ...seen.map((o) => ({
      value: o.value,
      note: o.note ?? people(o.count),
    })),
    ...extra.map((v) => ({
      value: v,
      note: flagsUnseen ? "not seen yet" : "added",
    })),
  ];

  return (
    <div
      ref={wrap}
      className="relative min-w-0"
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          close();
        }
      }}
    >
      <div
        className={cn(
          "border-input bg-card rounded-control relative flex min-h-8 flex-wrap items-center gap-1.5 border py-[3px] pr-8 pl-1.5 max-sm:min-h-11",
          open && "border-ring",
        )}
        onClick={(e) => {
          if (e.target === e.currentTarget && !disabled) onOpenChange(!open);
        }}
      >
        {values.map((v) => {
          const unseen = flagsUnseen && !known(v);
          return (
            <span
              key={v}
              title={
                unseen
                  ? "Nobody has signed in with this yet. It must match exactly."
                  : undefined
              }
              className={cn(
                "rounded-badge text-label inline-flex max-w-full items-center gap-1 py-0.5 pr-1 pl-2 font-medium [overflow-wrap:anywhere]",
                unseen
                  ? "bg-warning-tint text-warning"
                  : "bg-accent text-accent-foreground",
              )}
            >
              {unseen && <AlertTriangle aria-hidden="true" className="size-3.5" />}
              {v}
              {!disabled && (
                <button
                  type="button"
                  aria-label={`Remove ${v}`}
                  className="inline-flex size-5 items-center justify-center rounded-sm hover:bg-black/10 max-sm:size-8"
                  onClick={() => onChange(values.filter((x) => x !== v))}
                >
                  <X aria-hidden="true" className="size-3.5" />
                </button>
              )}
            </span>
          );
        })}
        <button
          ref={trigger}
          type="button"
          role="combobox"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-label={pickerLabel(field)}
          disabled={disabled}
          className="text-muted-foreground text-meta min-h-6 min-w-16 flex-1 px-1.5 text-left max-sm:min-h-8"
          onClick={() => onOpenChange(!open)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              if (!open) onOpenChange(true);
              requestAnimationFrame(() => moveFocus(null, "first"));
            }
          }}
        >
          {values.length ? "" : pickerLabel(field)}
        </button>
        <ChevronDown
          aria-hidden="true"
          className="text-muted-foreground pointer-events-none absolute top-2 right-2 size-4 max-sm:top-3.5"
        />
      </div>
      {open && (
        <>
          {phone && (
            <div
              aria-hidden="true"
              className="bg-overlay fixed inset-0 z-40"
              onClick={() => onOpenChange(false)}
            />
          )}
          <div
            className={cn(
              "bg-popover text-popover-foreground rounded-panel border p-1 shadow-overlay",
              phone
                ? "fixed inset-x-3 bottom-3 z-50 max-h-[70vh] overflow-auto"
                : "absolute top-[calc(100%+4px)] left-0 z-20 max-w-[min(420px,calc(100vw-48px))] min-w-[min(320px,100%)]",
            )}
          >
            {options.length ? (
              <div
                role="listbox"
                id={listId}
                aria-multiselectable="true"
                aria-label={pickerLabel(field)}
              >
                {options.map((o, i) => {
                  const on = chosen(o.value);
                  return (
                    <div
                      key={o.value}
                      role="option"
                      aria-selected={on}
                      tabIndex={i === 0 ? 0 : -1}
                      className="text-meta hover:bg-accent focus-visible:bg-accent flex min-h-9 cursor-pointer items-center gap-2 rounded-sm p-2 outline-none max-sm:min-h-11"
                      onClick={() => toggle(o.value)}
                      onKeyDown={(e) => {
                        if (e.key === " " || e.key === "Enter") {
                          e.preventDefault();
                          toggle(o.value);
                        } else if (e.key === "ArrowDown") {
                          e.preventDefault();
                          moveFocus(e.currentTarget, 1);
                        } else if (e.key === "ArrowUp") {
                          e.preventDefault();
                          moveFocus(e.currentTarget, -1);
                        } else if (e.key === "Home") {
                          e.preventDefault();
                          moveFocus(e.currentTarget, "first");
                        } else if (e.key === "End") {
                          e.preventDefault();
                          moveFocus(e.currentTarget, "last");
                        }
                      }}
                    >
                      <span
                        aria-hidden="true"
                        className={cn(
                          "border-input flex size-4.5 shrink-0 items-center justify-center rounded-sm border",
                          on && "border-primary bg-primary text-primary-foreground",
                        )}
                      >
                        {on && <Check className="size-3.5" />}
                      </span>
                      <span className="min-w-0 [overflow-wrap:anywhere]">
                        {o.value}
                      </span>
                      <span className="text-muted-foreground text-label ml-auto whitespace-nowrap">
                        {o.note}
                      </span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-muted-foreground text-meta p-2">
                {noneSeen(field)}
              </p>
            )}
            <div className="mt-1 flex gap-1.5 border-t px-1 pt-2 pb-1">
              <Input
                ref={addInput}
                size="sm"
                value={typed}
                spellCheck={false}
                placeholder={placeholder}
                aria-label={placeholder}
                onChange={(e) => setTyped(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    add();
                  }
                }}
              />
              <Button variant="outline" onClick={add}>
                Add
              </Button>
              <Button variant="ghost" onClick={close}>
                Done
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
