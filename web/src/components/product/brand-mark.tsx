import { cn } from "@/lib/cn";

/** The Azizam mark: a fish drawn from two crossing arcs, in the chrome accent. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={cn("text-chrome-accent size-6 shrink-0", className)}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21.5 12C17.5 5.5 9.5 5 3 16.5" />
      <path d="M21.5 12C17.5 18.5 9.5 19 3 7.5" />
      <circle cx="16.25" cy="10.5" r="0.75" fill="currentColor" stroke="none" />
    </svg>
  );
}
