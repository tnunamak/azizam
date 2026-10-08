import { cn } from "@/lib/cn";

const hues = [
  "bg-person-1",
  "bg-person-2",
  "bg-person-3",
  "bg-person-4",
  "bg-person-5",
  "bg-person-6",
  "bg-person-7",
] as const;

/* The same hues at 16%, for surfaces that belong to a person (audio covers). */
const tints = [
  "bg-person-1/16",
  "bg-person-2/16",
  "bg-person-3/16",
  "bg-person-4/16",
  "bg-person-5/16",
  "bg-person-6/16",
  "bg-person-7/16",
] as const;

function hueIndex(name: string): number {
  const key = name.trim().toLowerCase();
  let hash = 5381;
  for (const char of key) hash = ((hash << 5) + hash + char.codePointAt(0)!) >>> 0;
  return hash % hues.length;
}

/** A person keeps one hue everywhere: the djb2 hash of their display name. */
export function personHue(name: string): (typeof hues)[number] {
  return hues[hueIndex(name)];
}

export function personTint(name: string): (typeof tints)[number] {
  return tints[hueIndex(name)];
}

export function personInitial(name: string): string {
  const first = name.trim().match(/\p{L}|\p{N}/u)?.[0];
  return first ? first.toLocaleUpperCase() : "?";
}

const sizes = {
  xs: "size-5 text-2xs",
  sm: "size-6 text-xs",
  md: "size-8 text-sm",
  lg: "size-10 text-base",
} as const;

/**
 * The household's visual unit: a disc with the person's initial in their hue.
 * Decorative by default; pass `label` when the mark is the only name shown.
 */
export function PersonMark({
  name,
  size = "sm",
  label,
  className,
}: {
  name: string;
  size?: keyof typeof sizes;
  label?: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "text-person-ink inline-flex shrink-0 items-center justify-center rounded-full font-semibold select-none",
        personHue(name),
        sizes[size],
        className,
      )}
      {...(label
        ? { role: "img", "aria-label": label }
        : { "aria-hidden": true })}
    >
      {personInitial(name)}
    </span>
  );
}
