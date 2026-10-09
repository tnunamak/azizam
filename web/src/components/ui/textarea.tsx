import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * Min 80 (96 below 640 px), text `text-body`. `cn` does not merge classes, so
 * a caller's own min height or text size replaces these at every width.
 */
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  const ownMin = /(?:^|\s)min-h-/.test(className ?? "");
  const ownSize = /(?:^|\s)text-(?:2xs|xs|sm|base|lg|xl|body|meta|label|badge)\b/.test(
    className ?? "",
  );
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "border-input placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 rounded-control flex field-sizing-content w-full border bg-transparent px-3 py-2 transition-[color,box-shadow] outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-50",
        ownMin ? null : "min-h-20 max-sm:min-h-24",
        ownSize ? null : "text-body",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
