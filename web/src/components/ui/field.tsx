"use client";

import { createContext, useContext, useId } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface FieldContextValue {
  id: string;
  describedBy?: string;
  invalid: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

/** Inputs read this to wire id / aria-describedby / aria-invalid without
 * boolean-prop plumbing. Null outside a Field. */
export function useFieldContext(): FieldContextValue | null {
  return useContext(FieldContext);
}

export interface FieldProps {
  label: ReactNode;
  /** Quiet guidance below the control; hidden while error is shown. */
  help?: ReactNode;
  /** Validation message; switches the control into its invalid state. */
  error?: ReactNode;
  required?: boolean;
  className?: string;
  children: ReactNode;
}

/** Label + control + help/error, with accessibility wiring via context. */
export function Field({
  label,
  help,
  error,
  required = false,
  className,
  children,
}: FieldProps) {
  const id = useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const describedBy = error ? errorId : help ? helpId : undefined;

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-sm text-foreground font-medium">
        {label}
        {required && (
          <span aria-hidden="true" className="text-danger">
            {" *"}
          </span>
        )}
      </label>
      <FieldContext.Provider value={{ id, describedBy, invalid: !!error }}>
        {children}
      </FieldContext.Provider>
      {help && !error && (
        <p id={helpId} className="text-sm text-muted-foreground">
          {help}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
