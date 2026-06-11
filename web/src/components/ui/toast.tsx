"use client";

import { Toaster as SonnerToaster, toast } from "sonner";
import type { ComponentProps } from "react";

/** The one toast system (sonner), restyled entirely through brand tokens
 * via unstyled classNames. Import `toast` from here, never from sonner
 * directly, so the system stays swappable. */
export { toast };

export function Toaster(props: ComponentProps<typeof SonnerToaster>) {
  return (
    <SonnerToaster
      position="bottom-right"
      gap={8}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast:
            "flex w-80 items-start gap-3 rounded-vf-md border border-vf-border bg-vf-surface-2 p-4 text-vf-text shadow-vf-caustic-2",
          icon: "shrink-0",
          content: "flex min-w-0 flex-col gap-0.5",
          title: "text-vf-small font-medium text-vf-text",
          description: "text-vf-small text-vf-muted",
          actionButton:
            "ml-auto shrink-0 rounded-vf-sm bg-vf-accent px-2 py-1 text-vf-micro font-medium text-vf-accent-ink hover:bg-vf-accent/85",
          cancelButton:
            "ml-auto shrink-0 rounded-vf-sm px-2 py-1 text-vf-micro text-vf-muted hover:bg-vf-surface-3",
          success: "[&_[data-icon]]:text-vf-ok",
          error: "border-vf-bad/40 [&_[data-icon]]:text-vf-bad",
          warning: "border-vf-warn/40 [&_[data-icon]]:text-vf-warn",
          info: "[&_[data-icon]]:text-vf-info",
        },
      }}
      {...props}
    />
  );
}
