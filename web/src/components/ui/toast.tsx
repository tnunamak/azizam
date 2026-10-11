"use client";

import { lazy, Suspense, type ComponentProps } from "react";

const SonnerToaster = lazy(() =>
  import("./sonner").then((module) => ({ default: module.Toaster })),
);

/** The one toast system (sonner), restyled entirely through brand tokens
 * via unstyled classNames. Import `toast` from here, never from sonner
 * directly, so the system stays swappable. */
export { toast } from "@/lib/toast";

export function Toaster(
  props: ComponentProps<typeof import("sonner").Toaster>,
) {
  return (
    <Suspense fallback={null}>
      <SonnerToaster
        position="bottom-right"
        gap={8}
        toastOptions={{
          unstyled: true,
          classNames: {
          toast:
            "flex w-80 items-start gap-3 rounded-md border border-border bg-muted p-4 text-foreground",
          icon: "shrink-0",
          content: "flex min-w-0 flex-col gap-0.5",
          title: "text-sm font-medium text-foreground",
          description: "text-sm text-muted-foreground",
          actionButton:
            "ml-auto shrink-0 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/85",
          cancelButton:
            "ml-auto shrink-0 rounded-md px-2 py-1 text-xs text-muted-foreground",
          success: "[&_[data-icon]]:text-success",
          error: "border-danger/40 [&_[data-icon]]:text-danger",
          warning: "border-warning/40 [&_[data-icon]]:text-warning",
          info: "[&_[data-icon]]:text-info",
        },
        }}
        {...props}
      />
    </Suspense>
  );
}
