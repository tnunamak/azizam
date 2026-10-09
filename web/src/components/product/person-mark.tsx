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

/* The same hues as a custom property, so a surface that belongs to a person
   (an audio cover) can wash, draw and fill with that person's colour. */
const hueVars = [
  "[--person:var(--person-1)]",
  "[--person:var(--person-2)]",
  "[--person:var(--person-3)]",
  "[--person:var(--person-4)]",
  "[--person:var(--person-5)]",
  "[--person:var(--person-6)]",
  "[--person:var(--person-7)]",
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

/** Sets `--person` to the person's hue for everything inside. */
export function personHueVar(name: string): (typeof hueVars)[number] {
  return hueVars[hueIndex(name)];
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
