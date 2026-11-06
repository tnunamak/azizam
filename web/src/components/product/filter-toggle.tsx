"use client";

import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export function FilterToggle({
  pressed,
  children,
  className,
  ...props
}: Omit<React.ComponentProps<typeof Button>, "variant" | "aria-pressed"> & {
  pressed: boolean;
}) {
  return (
    <Button
      {...props}
      type="button"
      variant="outline"
      aria-pressed={pressed}
      className={cn(
        "aria-pressed:border-primary/30 aria-pressed:bg-accent aria-pressed:text-accent-foreground gap-2 shadow-none",
        className,
      )}
    >
      <Check
        aria-hidden="true"
        className={cn("size-4", !pressed && "invisible")}
      />
      {children}
    </Button>
  );
}
