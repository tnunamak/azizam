import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";
import { Slot } from "radix-ui";

/**
 * Two desktop heights (DESIGN "Controls"): 32 for page, toolbar, row and card
 * actions (the default, also `sm`), and 40 (`md`) for a form's submit, Try's
 * Run and a dialog's primary. A submit button with no size is `md`, so a form
 * keeps its 40 action beside its 40 fields. Below 640 px every size is at
 * least 44 × 44.
 * `lg` and `icon-lg` are 40 on desktop and 44 on phones.
 */
const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 rounded-control text-meta whitespace-nowrap transition-colors duration-(--motion-fast) ease-standard max-sm:min-h-11 max-sm:min-w-11 disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary/90",
        // The Home's Make colour (`--make`). The sign-in page's primary action uses it, so the two match.
        make: "bg-make text-make-foreground hover:bg-make/90",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        // In dark the edge is the field edge (`--input`, 3:1 on the page). Light keeps the hairline (`--border`).
        outline:
          "border border-border bg-card text-foreground hover:border-input hover:bg-muted dark:border-input",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-accent hover:text-accent-foreground",
        ghost: "text-foreground hover:bg-muted",
        link: "text-link underline-offset-4 hover:underline",
      },
      // The weight sits in each size, not the base, so `xl` can be semibold without a second font-weight class beside it
      // (cn does not merge conflicting utilities).
      size: {
        default: "h-8 gap-1.5 px-3 font-medium has-[>svg]:px-2.5",
        xs: "h-8 gap-1 px-2 font-medium text-badge has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-8 gap-1.5 px-3 font-medium has-[>svg]:px-2.5",
        md: "h-10 px-4 font-medium text-body has-[>svg]:px-3.5",
        // Large actions use the form height on desktop; 44 is phone-only.
        lg: "h-10 max-sm:h-11 px-5 font-medium text-body has-[>svg]:px-4",
        // The one primary action of a page that has nothing else to do (the sign-in page): 48 at every width, 16 px semibold.
        // `text-lg!` is important because the base's `text-meta` otherwise wins the cascade (14 px, measured).
        xl: "h-12 px-6 text-lg! font-semibold has-[>svg]:px-5",
        icon: "size-8 font-medium",
        "icon-xs": "size-8 font-medium [&_svg:not([class*='size-'])]:size-3.5",
        "icon-sm": "size-8 font-medium",
        "icon-lg": "size-10 font-medium max-sm:size-11",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant = "default",
  size: requestedSize,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "button";
  const size = requestedSize ?? (props.type === "submit" ? "md" : "default");

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
