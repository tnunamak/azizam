import Link from "next/link";
import { cn } from "@/lib/cn";

const views = [
  { value: "models", label: "Models", href: "/models" },
  { value: "workflows", label: "Workflows", href: "/workflows" },
] as const;

/** Discovery's peer views, styled like Segmented but using page links. */
export function DiscoveryTabs({ current }: { current: "models" | "workflows" }) {
  return (
    <nav
      aria-label="Discovery"
      className="inline-flex max-w-full items-center gap-1 overflow-x-auto sm:h-8"
    >
      {views.map((view) => (
        <Link
          key={view.value}
          href={view.href}
          aria-current={current === view.value ? "page" : undefined}
          className={cn(
            "hover:text-foreground aria-[current=page]:bg-accent aria-[current=page]:text-accent-foreground text-meta focus-visible:-outline-offset-4 flex shrink-0 items-center gap-1.5 font-medium whitespace-nowrap transition-colors duration-(--motion-fast) aria-[current=page]:font-semibold max-sm:min-h-11 max-sm:min-w-11",
            "text-muted-foreground hover:bg-muted rounded-control px-2.5 sm:h-7",
            "max-sm:flex-1 max-sm:justify-center max-sm:px-2",
          )}
        >
          <span data-slot="segment-word">{view.label}</span>
        </Link>
      ))}
    </nav>
  );
}
